"""The public demo: examples decided from recorded real responses, public info, live mode guarded and off by default."""

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from priorauth.demo.examples import EXAMPLES
from priorauth.demo.live import LiveConfig
from priorauth.demo.server import DEFAULT_REVIEWER_PASSWORD, DemoConfig, DemoStartError, demo_app
from priorauth.jobs.db import CaseRow
from priorauth.llm.fake import FAKE_USAGE

ROOT = Path(__file__).resolve().parents[1]
CONFIG = DemoConfig(data_dir=ROOT / "data", cassettes_dir=ROOT / "eval/cassettes")
SYNTHETIC_RECORD = (
    "PRIOR AUTHORIZATION REQUEST - OUTPATIENT IMAGING\nRequested service: MRI lumbar spine\n\f"
    "Spine Consultation\nLow back pain for 8 weeks. Callback (312) 555-0199."
)


class Scripted:
    """A stand-in model for the live route: says INSUFFICIENT for every criterion, or fails."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[dict[str, Any]] = []

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if self.fail:
            raise ValueError("model unavailable")
        criteria = [
            ln[2:].split(":")[0] for ln in request["messages"][0]["content"].splitlines() if ln.startswith("- ")
        ]
        calls = [("record_finding", {"criterion_id": c, "status": "INSUFFICIENT", "evidence": []}) for c in criteria]
        calls.append(("finish", {"injection_detected": False}))
        content = [{"type": "tool_use", "id": f"t{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)]
        return {
            "type": "message",
            "content": content,
            "stop_reason": "tool_use",
            "usage": FAKE_USAGE | {"output_tokens": 50},
        }


@asynccontextmanager
async def demo(config: DemoConfig = CONFIG) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    async with (
        demo_app(config) as app,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as client,
    ):
        yield client, app


async def reviewer(client: httpx.AsyncClient, password: str = DEFAULT_REVIEWER_PASSWORD) -> dict[str, str]:
    r = await client.post("/auth/token", json={"username": "demo-reviewer", "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def decided(client: httpx.AsyncClient, case_id: str, headers: dict[str, str]) -> dict[str, Any]:
    for _ in range(400):
        view: dict[str, Any] = (await client.get(f"/cases/{case_id}", headers=headers)).json()
        if view["job"]["status"] in ("done", "failed"):
            return view
        await asyncio.sleep(0.02)
    raise AssertionError("not decided")


def test_examples_cover_the_traps_and_are_decided_as_labelled() -> None:
    assert len(EXAMPLES) >= 8
    labels = " ".join(e.label for e in EXAMPLES).lower()
    for trap in ("dose x4", "left/right", "inconsistent dates", "hidden instruction", "to approve", "to pend"):
        assert trap in labels, trap

    async def scenario() -> None:
        async with demo() as (client, _):
            headers = await reviewer(client)
            examples = (await client.get("/demo/examples")).json()  # public
            assert len(examples) == len(EXAMPLES)
            for ex in examples:
                view = await decided(client, ex["seeded_case_id"], headers)
                assert view["outcome"] == ex["expected_decision"], ex["id"]
                review = (await client.get(f"/cases/{ex['seeded_case_id']}/review", headers=headers)).json()
                assert review["extracted_by"].startswith("agent_v2_haiku+protected (replayed")
                if ex["expected_decision"] == "PEND":
                    assert review["reasons"]
            injection = next(e for e in examples if e["id"] == "injection")
            review = (await client.get(f"/cases/{injection['seeded_case_id']}/review", headers=headers)).json()
            assert review["injection_detected"] and "INJECTION_DETECTED" in [r["code"] for r in review["reasons"]]

    asyncio.run(scenario())


def test_running_an_example_goes_through_the_queue_and_gives_the_recorded_decision() -> None:
    async def scenario() -> None:
        async with demo() as (client, _):
            assert (await client.post("/demo/examples/injection/run")).status_code == 401  # a token is needed
            headers = await reviewer(client)
            for example_id, expected in (("injection", "PEND"), ("clean-approve-pt", "APPROVE")):
                r = await client.post(f"/demo/examples/{example_id}/run", headers=headers)
                assert r.status_code == 202
                assert (await decided(client, r.json()["case_id"], headers))["outcome"] == expected
            assert (await client.post("/demo/examples/nope/run", headers=headers)).status_code == 404

    asyncio.run(scenario())


def test_only_the_reviewer_account_is_public() -> None:
    async def scenario() -> None:
        async with demo() as (client, _):
            info = (await client.get("/demo/info")).json()
            assert (info["reviewer_username"], info["reviewer_password"]) == (
                "demo-reviewer",
                DEFAULT_REVIEWER_PASSWORD,
            )
            assert info["live_enabled"] is False and info["resets_on_restart"] is True
            assert "synthetic" in info["notice"].lower()
            for username in ("demo-admin", "demo-system"):
                r = await client.post("/auth/token", json={"username": username, "password": DEFAULT_REVIEWER_PASSWORD})
                assert r.status_code == 401
            headers = await reviewer(client)
            assert (await client.get("/audit/verify", headers=headers)).status_code == 403
            assert (await client.post("/cases", json={}, headers=headers | {"Idempotency-Key": "x"})).status_code == 403
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.get("/docs")).status_code == 200

    asyncio.run(scenario())


def test_live_mode_is_off_by_default() -> None:
    async def scenario() -> None:
        async with demo() as (client, _):
            headers = await reviewer(client)
            r = await client.post(
                "/demo/live", json={"procedure_code": "MRI_LUMBAR", "record_text": SYNTHETIC_RECORD}, headers=headers
            )
            assert r.status_code == 404 and "disabled" in r.json()["detail"]

    asyncio.run(scenario())


def test_live_mode_caps_and_keeps_nothing() -> None:
    model = Scripted()
    config = DemoConfig(
        data_dir=CONFIG.data_dir,
        cassettes_dir=CONFIG.cassettes_dir,
        live=LiveConfig(enabled=True, max_chars=400, per_ip_per_hour=2, daily_cap_usd=1.0),
        live_transport=lambda: model,
    )

    async def scenario() -> None:
        async with demo(config) as (client, app):
            headers = await reviewer(client)
            async with app.state.sessions() as s:
                before = (await s.execute(select(func.count()).select_from(CaseRow))).scalar_one()
            body = {"procedure_code": "MRI_LUMBAR", "record_text": SYNTHETIC_RECORD}
            r = await client.post("/demo/live", json=body, headers=headers)
            assert r.status_code == 200, r.text
            result = r.json()
            assert result["outcome"] == "PEND" and len(result["pages"]) == 2 and result["criteria"]
            assert "555-0199" not in str(model.requests)  # phone numbers are redacted before the model
            async with app.state.sessions() as s:
                after = (await s.execute(select(func.count()).select_from(CaseRow))).scalar_one()
                audit_text = str((await s.execute(select(func.count()))).all())
            assert after == before  # nothing stored
            assert "Low back pain" not in audit_text
            too_long = {"procedure_code": "MRI_LUMBAR", "record_text": "x" * 401}
            assert (await client.post("/demo/live", json=too_long, headers=headers)).status_code == 413
            assert (await client.post("/demo/live", json=body, headers=headers)).status_code == 200
            assert (
                await client.post("/demo/live", json=body, headers=headers)
            ).status_code == 429  # 2 per hour; the 413 did not count
            admin_entries = app.state.audit
            entries = await admin_entries.entries(limit=1000)
            live = [e for e in entries if e.action == "demo.live"]
            assert [e.detail.get("status") for e in live] == [200, 413, 200, 429]
            assert "Low back pain" not in str([e.model_dump() for e in entries])

    asyncio.run(scenario())


@pytest.mark.parametrize(("fail", "daily_cap", "status"), [(True, 1.0, 502), (False, 0.000001, 503)])
def test_live_mode_fails_closed(fail: bool, daily_cap: float, status: int) -> None:
    config = DemoConfig(
        data_dir=CONFIG.data_dir,
        cassettes_dir=CONFIG.cassettes_dir,
        live=LiveConfig(enabled=True, daily_cap_usd=daily_cap),
        live_transport=lambda: Scripted(fail=fail),
    )

    async def scenario() -> None:
        async with demo(config) as (client, _):
            headers = await reviewer(client)
            body = {"procedure_code": "MRI_LUMBAR", "record_text": SYNTHETIC_RECORD}
            r = await client.post("/demo/live", json=body, headers=headers)
            assert r.status_code == status and "examples" in r.json()["detail"]
            assert "outcome" not in r.json()  # no partial or fallback decision

    asyncio.run(scenario())


def test_the_demo_refuses_to_start_without_recorded_responses(tmp_path: Path) -> None:
    (tmp_path / "cassettes").mkdir()
    shutil.copytree(ROOT / "eval/cassettes/agent_v1_haiku", tmp_path / "cassettes/agent_v1_haiku")  # other prompts
    config = DemoConfig(data_dir=CONFIG.data_dir, cassettes_dir=tmp_path / "cassettes")

    async def scenario() -> None:
        async with demo(config):
            pass

    with pytest.raises(DemoStartError, match="no recorded model response"):
        asyncio.run(scenario())


def test_the_smoke_test_passes_on_the_demo_and_catches_an_approved_injection() -> None:
    from priorauth.demo import smoke

    async def scenario() -> None:
        async with demo() as (client, app):
            lines = await smoke.smoke(client)
            assert any("injection: PEND" in line for line in lines)
            # Break the demo on purpose: the hidden-instruction example now points at a clean record.
            state = app.state.demo
            clean = next(item for item in state.examples if item[0].id == "clean-approve-pt")
            state.examples = [
                (ex, clean[1], clean[2]) if ex.id == "injection" else (ex, case, gold)
                for ex, case, gold in state.examples
            ]
            with pytest.raises(smoke.SmokeFailure, match="hidden-instruction record was APPROVED"):
                await smoke.smoke(client)

    asyncio.run(scenario())
