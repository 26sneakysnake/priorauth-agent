"""Hash-chained audit log: what is recorded, and that tampering of any kind is detected."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import delete, text, update

from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import Base, create_schema, make_engine, sessions
from priorauth.security.audit import AuditHead, AuditLog, AuditRow, ForbiddenDetail, chain_hash
from tests.test_api import DEV, api, login, post, wait_done
from tests.test_jobs import BACKENDS, POSTGRES

KEY = b"audit-key-for-unit-tests"


@pytest.fixture(params=BACKENDS)
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}"
    assert POSTGRES is not None
    return POSTGRES


@asynccontextmanager
async def audit_log(url: str, key: bytes | None = KEY) -> AsyncIterator[AuditLog]:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    log = AuditLog(sessions(engine), key)
    await log.init()
    try:
        yield log
    finally:
        await engine.dispose()


async def fill(log: AuditLog, n: int = 5) -> None:
    for i in range(n):
        await log.record(f"user-{i}", "reviewer", "case.read", f"case-{i}", status=200)


def test_an_untouched_chain_verifies(db_url: str) -> None:
    async def scenario() -> None:
        async with audit_log(db_url) as log:
            assert (await log.verify()).ok  # empty
            await fill(log)
            report = await log.verify()
            assert (report.ok, report.entries, report.keyed) == (True, 5, True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tamper", "reason", "seq"),
    [
        ("modify", "hash_mismatch", 3),
        ("modify_and_rehash_without_key", "hash_mismatch", 3),
        ("delete_middle", "missing_entry", 3),
        ("delete_last", "head_mismatch", 5),
        ("relink", "broken_link", 4),
        ("rewind_head", "head_mismatch", 6),
    ],
)
def test_tampering_is_detected(db_url: str, tamper: str, reason: str, seq: int) -> None:
    async def scenario() -> None:
        async with audit_log(db_url) as log:
            await fill(log)
            async with log.sessions.begin() as s:
                if tamper == "modify":
                    await s.execute(update(AuditRow).where(AuditRow.seq == 3).values(actor_id="someone-else"))
                elif tamper == "modify_and_rehash_without_key":
                    row = await s.get(AuditRow, 3)
                    assert row is not None
                    row.action = "case.submit"
                    row.hash = chain_hash(None, row.prev_hash, row)  # what an attacker without the key can do
                elif tamper == "delete_middle":
                    await s.execute(delete(AuditRow).where(AuditRow.seq == 3))
                elif tamper == "delete_last":
                    await s.execute(delete(AuditRow).where(AuditRow.seq == 5))
                elif tamper == "relink":
                    await s.execute(update(AuditRow).where(AuditRow.seq == 4).values(prev_hash="f" * 64))
                elif tamper == "rewind_head":
                    await s.execute(update(AuditHead).values(seq=4))
            report = await log.verify()
            assert (report.ok, report.reason, report.first_bad_seq) == (False, reason, seq)

    asyncio.run(scenario())


def test_without_a_key_the_report_says_so(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with audit_log(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", key=None) as log:
            await fill(log, 2)
            report = await log.verify()
            assert report.ok and not report.keyed

    asyncio.run(scenario())


def test_details_are_allowlisted_scalars() -> None:
    log = AuditLog(None, KEY)  # type: ignore[arg-type]  # rejected before any database access
    with pytest.raises(ForbiddenDetail, match="not allowlisted"):
        asyncio.run(log.record("u", "reviewer", "review.flag", "case", comment="the patient said..."))
    with pytest.raises(ForbiddenDetail, match="type or length"):
        asyncio.run(log.record("u", "reviewer", "review.flag", "case", reason="x" * 81))
    with pytest.raises(ForbiddenDetail, match="type or length"):
        asyncio.run(log.record("u", "reviewer", "review.flag", "case", reason=["a"]))


def test_concurrent_writers_keep_one_contiguous_chain(db_url: str) -> None:
    """Two AuditLog instances on one database stand for two processes: no shared lock, only the database."""

    async def scenario() -> None:
        async with audit_log(db_url) as first:
            second = AuditLog(first.sessions, KEY)
            await asyncio.gather(
                *(log.record("w", "system", "job.decide", f"case-{i}") for i in range(30) for log in (first, second))
            )
            report = await first.verify()
            assert (report.ok, report.entries) == (True, 60)
            entries = await first.entries(limit=1000)
            assert [e.seq for e in entries] == list(range(1, 61))

    asyncio.run(scenario())


def test_the_api_records_accesses_refusals_and_decisions(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="system") as (client, _):
            case_id = (await post(client, case_to_bundle(DEV[0][0]), key="audited")).json()["case_id"]
            await wait_done(client, case_id)
            await client.get(f"/cases/{case_id}/result")
            assert (await client.get("/costs/daily")).status_code == 403
            assert (await client.get("/cases/x", headers={"Authorization": "Bearer nope"})).status_code == 401
            await client.post("/auth/token", json={"username": "demo-admin", "password": "not the password"})
            admin = {"Authorization": f"Bearer {await login(client, 'demo-admin')}"}
            report = (await client.get("/audit/verify", headers=admin)).json()
            entries = (await client.get("/audit", headers=admin, params={"limit": 1000})).json()
        assert report["ok"] and report["keyed"] and report["entries"] >= 8
        actions = [e["action"] for e in entries]
        for expected in ("auth.login", "case.submit", "job.decide", "case.read", "result.read", "access.denied",
                         "auth.failed"):  # fmt: skip
            assert expected in actions, expected
        decide = next(e for e in entries if e["action"] == "job.decide")
        assert decide["resource"] == case_id and decide["detail"]["outcome"] in ("APPROVE", "PEND")
        denied = [e["detail"] for e in entries if e["action"] == "access.denied"]
        assert {"status": 403, "reason": "role"} in denied and {"status": 401, "reason": "token_invalid"} in denied
        submit = next(e for e in entries if e["action"] == "case.submit")
        assert submit["actor_role"] == "system" and len(submit["actor_id"]) == 36  # the user's unique id
        assert DEV[0][0].patient.name not in json.dumps(entries)

    asyncio.run(scenario())


def test_raw_audit_rows_are_plain_but_hold_no_record_content(tmp_path: Path) -> None:
    """The audit log is not encrypted (it must stay verifiable); it only ever holds ids, codes and outcomes."""

    async def scenario() -> list[str]:
        async with api(tmp_path) as (client, _):
            for case, _gold in DEV[:3]:
                cid = (await post(client, case_to_bundle(case), key=case.case_id)).json()["case_id"]
                await wait_done(client, cid)
            app_sessions = client._transport.app.state.sessions  # type: ignore[attr-defined]
            async with app_sessions() as s:
                return [json.dumps(list(r)) for r in (await s.execute(text("SELECT * FROM audit_log"))).all()]

    rows = "\n".join(asyncio.run(scenario()))
    for case, _gold in DEV[:3]:
        for secret in (case.patient.name, case.patient.member_id, case.patient.mrn, case.pages[1].text[:40]):
            assert secret not in rows
