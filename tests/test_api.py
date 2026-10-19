"""HTTP API, end to end: FHIR Bundle -> job -> worker (scripted fake model) -> ClaimResponse -> signed webhook."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fhir.resources.R4B.claimresponse import ClaimResponse
from sqlalchemy import func, select

from priorauth.agent.loop import AgentSystem
from priorauth.api import openapi
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.data.schema import Case, Gold, Trap
from priorauth.jobs import webhooks
from priorauth.jobs.db import JobRow
from priorauth.llm.fake import fake_client, protected_oracle
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import System
from priorauth.security.model_view import ProtectedSystem
from tests.test_data import load
from tests.test_jobs import Blocking

DEV = load("dev")
SECRET = "hook-secret"
HOOK_URL = "https://receiver.example/priorauth"
JWT_SECRET = "jwt-secret-for-tests-only-0123456789"
AUDIT_KEY = "audit-key-for-tests-only"
DEMO_PASSWORD = "demo-password-for-tests"


def one_per_trap() -> list[tuple[Case, Gold]]:
    seen: dict[Trap, tuple[Case, Gold]] = {}
    for case, gold in DEV:
        seen.setdefault(gold.trap, (case, gold))
    return list(seen.values())


def oracle_system() -> System:
    """The production wiring (agent behind the model view) with the scripted oracle in place of the model."""
    return ProtectedSystem(AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(protected_oracle(DEV))))


async def login(client: httpx.AsyncClient, username: str, password: str = DEMO_PASSWORD) -> str:
    r = await client.post("/auth/token", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    token: str = r.json()["access_token"]
    return token


@asynccontextmanager
async def api(
    tmp_path: Path, system_factory: Callable[[], System] = oracle_system, role: str | None = "admin", **settings: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, list[httpx.Request]]]:
    """The app with the demo users; the client carries a token of `role` (None: no token)."""
    received: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(204)

    hooks = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
    config: dict[str, Any] = {
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        "workers": 3,
        "jwt_secret": JWT_SECRET,
        "audit_key": AUDIT_KEY,
        "demo_password": DEMO_PASSWORD,
    } | settings
    app = create_app(Settings(**config), system_factory=system_factory, webhook_client=hooks)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            if role is not None:
                client.headers["Authorization"] = f"Bearer {await login(client, f'demo-{role}')}"
            yield client, received
    await hooks.aclose()


async def post(client: httpx.AsyncClient, body: Any, key: str | None = "key-1") -> httpx.Response:
    headers = {"Idempotency-Key": key} if key else {}
    return await client.post("/cases", json=body, headers=headers)


async def wait_done(
    client: httpx.AsyncClient, case_id: str, timeout: float = 15.0, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        view: dict[str, Any] = (await client.get(f"/cases/{case_id}", headers=headers)).json()
        if view["job"]["status"] in ("done", "failed"):
            return view
        assert asyncio.get_running_loop().time() < deadline, view
        await asyncio.sleep(0.05)


def test_end_to_end_bundle_to_claim_response_and_signed_webhook(tmp_path: Path) -> None:
    cases = one_per_trap()

    async def scenario() -> None:
        async with api(tmp_path, webhook_url=HOOK_URL, webhook_secret=SECRET) as (client, received):
            accepted = []
            for case, _ in cases:
                r = await post(client, case_to_bundle(case, case.case_id), key=f"idem-{case.case_id}")
                assert r.status_code == 202, r.text
                assert r.headers["location"] == f"/cases/{r.json()['case_id']}"
                accepted.append(r.json())
            for (case, gold), acc in zip(cases, accepted, strict=True):
                view = await wait_done(client, acc["case_id"])
                assert view["job"]["status"] == "done" and view["outcome"] == gold.expected_decision
                assert view["submitter_case_id"] == case.case_id
                result = await client.get(acc["result_url"])
                assert result.status_code == 200
                assert result.headers["content-type"].startswith("application/fhir+json")
                claim = result.json()
                ClaimResponse.model_validate(claim)
                assert claim["outcome"] == ("complete" if gold.expected_decision == "APPROVE" else "queued")
            deadline = asyncio.get_running_loop().time() + 10
            while len(received) < len(cases):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.05)
        record_text = [p.text for c, _ in cases for p in c.pages] + [c.patient.name for c, _ in cases]
        outcomes = {}
        for hook in received:
            assert webhooks.verify(SECRET.encode(), hook.headers[webhooks.SIGNATURE_HEADER], hook.content)
            payload = json.loads(hook.content)
            outcomes[payload["case_id"]] = payload["outcome"]
            assert not any(t in hook.content.decode() for t in record_text)  # identifiers and outcome only
        assert outcomes == {a["case_id"]: g.expected_decision for (_, g), a in zip(cases, accepted, strict=True)}

    asyncio.run(scenario())


def test_invalid_bundles_get_explicit_422s(tmp_path: Path) -> None:
    case = DEV[0][0]

    async def scenario() -> None:
        async with api(tmp_path, run_workers=False) as (client, _):
            r = await client.post(
                "/cases", content=b"{not json", headers={"Idempotency-Key": "k", "content-type": "application/json"}
            )
            assert r.status_code == 422
            r = await post(client, {"resourceType": "Patient"})
            assert r.status_code == 422 and "FHIR Bundle" in r.json()["detail"][0]["msg"]
            bad = case_to_bundle(case)
            bad["entry"][0]["resource"]["birthDate"] = "yesterday"
            bad["type"] = "batch"
            r = await post(client, bad)
            assert r.status_code == 422
            assert r.json()["detail"][0]["loc"][:4] == ["entry", 0, "resource", "birthDate"]
            no_patient = case_to_bundle(case)
            no_patient["entry"].pop(0)
            no_patient["type"] = "batch"
            r = await post(client, no_patient)
            messages = [d["msg"] for d in r.json()["detail"]]
            assert r.status_code == 422 and len(messages) == 2
            assert any("exactly one Patient" in m for m in messages)
            assert any("collection" in m for m in messages)
            r = await post(client, case_to_bundle(case), key=None)
            assert r.status_code == 400 and "Idempotency-Key" in r.json()["detail"]

    asyncio.run(scenario())


def test_idempotency_key_replays_and_conflicts(tmp_path: Path) -> None:
    a, b = DEV[0][0], DEV[1][0]

    async def scenario() -> None:
        async with api(tmp_path, run_workers=False) as (client, _):
            first = await post(client, case_to_bundle(a), key="same")
            again = await post(client, case_to_bundle(a), key="same")
            assert first.status_code == again.status_code == 202
            assert first.json()["job_id"] == again.json()["job_id"]
            assert again.json()["status"] == "accepted (replayed key)"
            conflict = await post(client, case_to_bundle(b), key="same")
            assert conflict.status_code == 409
            racing = await asyncio.gather(*(post(client, case_to_bundle(b), key="race") for _ in range(5)))
            assert {r.status_code for r in racing} == {202} and len({r.json()["job_id"] for r in racing}) == 1
            pending = await client.get(f"/cases/{first.json()['case_id']}/result")
            assert pending.status_code == 409 and pending.json()["detail"] == "job is queued"
            assert (await client.get("/cases/does-not-exist")).status_code == 404

    asyncio.run(scenario())


def test_job_survives_a_worker_crash_through_the_api(tmp_path: Path) -> None:
    case, gold = DEV[0]

    async def scenario() -> None:
        blocking = Blocking()
        async with api(tmp_path, system_factory=lambda: blocking, lease_seconds=0.5) as (client, _):
            r = await post(client, case_to_bundle(case))
            case_id = r.json()["case_id"]
            await asyncio.wait_for(blocking.started.wait(), timeout=5)
            app_pool = client._transport.app.state.pool  # type: ignore[attr-defined]
            for task in list(app_pool.tasks):  # kill the job in flight, as a process crash would
                task.cancel()
            await asyncio.gather(*app_pool.tasks, return_exceptions=True)
        # A new API process (new workers, same database file) picks the job up once the lease has expired.
        await asyncio.sleep(0.6)
        async with api(tmp_path, lease_seconds=0.5) as (client, _):
            view = await wait_done(client, case_id)
            assert view["job"]["attempts"] == 2 and view["outcome"] == gold.expected_decision

    asyncio.run(scenario())


def test_failed_job_result_is_a_fail_closed_pend(tmp_path: Path) -> None:
    case = DEV[0][0]

    async def scenario() -> None:
        async with api(tmp_path, run_workers=False) as (client, _):
            case_id = (await post(client, case_to_bundle(case))).json()["case_id"]
            sessions = client._transport.app.state.sessions  # type: ignore[attr-defined]
            async with sessions.begin() as s:
                job = (await s.execute(select(JobRow).where(JobRow.case_id == case_id))).scalar_one()
                job.status, job.outcome, job.error = "failed", "PEND", "max_attempts: worker lost"
            claim = (await client.get(f"/cases/{case_id}/result")).json()
            assert claim["outcome"] == "queued"
            assert claim["extension"][0]["valueCodeableConcept"]["coding"][0]["code"] == "A4"
            async with sessions() as s:
                assert (await s.execute(select(func.count()).select_from(JobRow))).scalar_one() == 1

    asyncio.run(scenario())


def test_openapi_export_is_up_to_date() -> None:
    assert openapi.render() == openapi.OUTPUT.read_text(encoding="utf-8")
    spec = json.loads(openapi.render())
    assert {"/cases", "/cases/{case_id}", "/cases/{case_id}/result"} <= set(spec["paths"])
