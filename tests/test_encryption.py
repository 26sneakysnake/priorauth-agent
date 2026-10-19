"""Field encryption at rest: the raw database holds no record content, the application reads it back."""

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import CaseRow, JobRow
from priorauth.pipeline import CaseRun
from priorauth.security import crypto
from tests.test_api import api, one_per_trap, post, wait_done


def record_strings(cases: list) -> set[str]:  # type: ignore[type-arg]
    """What must not appear in clear: identifiers, every page line of 12+ characters, every gold quote."""
    out: set[str] = set()
    for case, gold in cases:
        out |= {case.patient.name, case.patient.mrn, case.patient.member_id}
        out |= {ln.strip() for p in case.pages for ln in p.text.splitlines() if len(ln.strip()) >= 12}
        out |= {e.quote for gc in gold.criteria.values() for e in gc.evidence}
    out.discard("SYNTHETIC RECORD - generated for software testing, not a real patient")  # footer; no content
    return {s for s in out if not s.startswith("Page ")}


def raw_cells(db: Path) -> list[str]:
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return [str(v) for t in tables for row in con.execute(f'SELECT * FROM "{t}"') for v in row if v is not None]  # noqa: S608
    finally:
        con.close()


def test_raw_database_holds_no_record_content(tmp_path: Path) -> None:
    cases = one_per_trap()

    async def scenario() -> None:
        async with api(tmp_path, webhook_url="https://receiver.example/h", webhook_secret="s") as (client, _):
            for case, _gold in cases:
                case_id = (await post(client, case_to_bundle(case, case.case_id), key=case.case_id)).json()["case_id"]
                await wait_done(client, case_id)
                assert (await client.get(f"/cases/{case_id}/result")).status_code == 200
            app_sessions = client._transport.app.state.sessions  # type: ignore[attr-defined]
            async with app_sessions() as s:  # the application reads the fields back in clear
                rows = (await s.execute(select(CaseRow.member_id, JobRow.result_json).join(JobRow))).all()
            assert {m for m, _ in rows} == {c.patient.member_id for c, _ in cases}
            assert all(CaseRun.model_validate_json(r).decision for _, r in rows)

    asyncio.run(scenario())
    cells = raw_cells(tmp_path / "api.db")
    blob = "\n".join(cells)
    leaked = sorted(s for s in record_strings(cases) if s in blob)
    assert leaked == []
    con = sqlite3.connect(tmp_path / "api.db")
    stored = con.execute("SELECT member_id, case_json, conditions_json FROM cases").fetchall()
    results = con.execute("SELECT result_json FROM jobs").fetchall()
    con.close()
    assert stored and all(v.startswith("gAAAAA") for row in stored + results for v in row)  # Fernet tokens
    # Control: the same scan over the decrypted cells finds the record, so a cleartext column would have failed it.
    clear = "\n".join(crypto.cipher().decrypt(c) if c.startswith("gAAAAA") else c for c in cells)
    identifiers = {x for c, _ in cases for x in (c.patient.name, c.patient.mrn, c.patient.member_id)}
    assert identifiers <= {s for s in record_strings(cases) if s in clear}


def test_without_a_key_nothing_is_stored_and_the_app_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(crypto.ENV)
    with pytest.raises(crypto.MissingKey):
        crypto.EncryptedText().process_bind_param("anything", None)  # type: ignore[arg-type]
    app = create_app(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}", run_workers=False))

    async def start() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(crypto.MissingKey):
        asyncio.run(start())
    assert not (tmp_path / "k.db").exists()


def test_key_rotation_reads_old_rows_and_wrong_keys_fail_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv(crypto.ENV, old)
    token = crypto.cipher().encrypt("Member ID: EHP-SYN-00000001")
    monkeypatch.setenv(crypto.ENV, f"{new},{old}")  # new key first: encrypts with new, still decrypts old
    assert crypto.cipher().decrypt(token) == "Member ID: EHP-SYN-00000001"
    assert Fernet(new.encode()).decrypt(crypto.cipher().encrypt("x").encode()) == b"x"
    monkeypatch.setenv(crypto.ENV, new)
    with pytest.raises(InvalidToken) as e:
        crypto.cipher().decrypt(token)
    assert "EHP-SYN" not in str(e.value) and token not in str(e.value)
    assert os.environ[crypto.ENV] == new
