"""Load test: every synthetic case submitted at once to the API, with a SIMULATED model.

    make load-test            # SQLite (a fresh file), 200 cases, 32 workers -> results/load_test.json
    make load-test-postgres   # the same against LOAD_TEST_DATABASE_URL (Postgres) -> results/load_test_postgres.json

What runs is the API's production code path: bearer token (demo `system` user), FHIR parsing, job queue and worker
pool, model view (minimum necessary + redaction), verifier and engine, field encryption (an ephemeral key unless
PRIORAUTH_FIELD_KEY is set) and the audit log. What does not: this is NOT a production deployment. No model is called
(priorauth.llm.fake.OracleTransport replays the gold labels with a seeded, simulated latency); the API runs in the
test process (httpx.ASGITransport: no network, no TLS, one process); the database is the one given by
--database-url, SQLite by default. It measures the API, the queue, the workers and the database under concurrency.

Output: its `deterministic` section (counts, outcomes, agreement with gold) is identical from run to run; its
`timing` section depends on the machine (`machine`) and the database (`settings.database`).
"""

import argparse
import asyncio
import json
import os
import platform
import secrets
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, text

from eval.metrics import distribution
from eval.run import load_split
from priorauth.agent.loop import AgentSystem
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import Base, CaseRow, JobRow, make_engine
from priorauth.llm.fake import fake_client, protected_oracle
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import System
from priorauth.security.crypto import ENV as FIELD_KEY_ENV
from priorauth.security.crypto import new_key
from priorauth.security.model_view import ProtectedSystem

OUTPUT = Path("results/load_test.json")


def machine() -> dict[str, Any]:
    mem_gb = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_gb": mem_gb,
        "python": sys.version.split()[0],
    }


async def describe_database(url: str) -> str:
    if url.startswith("sqlite"):
        return "SQLite (aiosqlite), a fresh file in a temporary directory"
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
    finally:
        await engine.dispose()
    pool = "SQLAlchemy default pool (5 connections + 10 overflow)"
    return f"PostgreSQL {version} (asyncpg), tables dropped and recreated before the run; {pool}"


async def reset_database(url: str) -> None:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    finally:
        await engine.dispose()


async def run(
    n_cases: int,
    workers: int,
    latency_s: float,
    jitter_s: float,
    seed: int,
    data: Path,
    database_url: str | None = None,
) -> dict[str, Any]:
    pairs = [p for split in ("dev", "test", "robust") for p in load_split(data, split)][:n_cases]
    golds = {case.case_id: gold for case, gold in pairs}
    transport = protected_oracle(pairs, latency_s=latency_s, jitter_s=jitter_s, seed=seed)
    os.environ.setdefault(FIELD_KEY_ENV, new_key())
    in_flight, peak = 0, 0
    inner = transport.send

    async def counted(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            return await inner(request)
        finally:
            in_flight -= 1

    transport.send = counted  # type: ignore[method-assign]

    def system() -> System:
        return ProtectedSystem(AgentSystem("agent_v2_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(transport)))

    password = secrets.token_urlsafe(16)
    with tempfile.TemporaryDirectory() as tmp:
        url = database_url or f"sqlite+aiosqlite:///{Path(tmp) / 'load.db'}"
        if database_url:
            await reset_database(url)
        database = await describe_database(url)
        settings = Settings(
            database_url=url,
            workers=workers,
            jwt_secret=secrets.token_urlsafe(32),
            audit_key=secrets.token_urlsafe(32),
            demo_password=password,
            review_feedback_path=None,
        )
        app = create_app(settings, system_factory=system)
        transport_ = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport_, base_url="http://api") as client,
        ):
            r = await client.post("/auth/token", json={"username": "demo-system", "password": password})
            r.raise_for_status()
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

            async def submit(case_id: str, bundle: dict[str, Any]) -> float:
                t = time.perf_counter()
                r = await client.post("/cases", json=bundle, headers={"Idempotency-Key": f"load-{case_id}"})
                r.raise_for_status()
                return (time.perf_counter() - t) * 1000

            start = time.perf_counter()
            submit_ms = await asyncio.gather(*(submit(c.case_id, case_to_bundle(c, c.case_id)) for c, _ in pairs))
            sessions = app.state.sessions
            while True:
                async with sessions() as s:
                    open_jobs = (
                        await s.execute(
                            select(func.count()).select_from(JobRow).where(JobRow.status.in_(("queued", "running")))
                        )
                    ).scalar_one()
                if open_jobs == 0:
                    break
                await asyncio.sleep(0.05)
            wall_s = time.perf_counter() - start
            async with sessions() as s:
                rows = (
                    await s.execute(
                        select(
                            CaseRow.submitter_case_id,
                            JobRow.status,
                            JobRow.outcome,
                            JobRow.created_at,
                            JobRow.finished_at,
                            JobRow.llm_calls,
                        ).join(JobRow, JobRow.case_id == CaseRow.id)
                    )
                ).all()
    statuses = Counter(r[1] for r in rows)
    outcomes = Counter(r[2] for r in rows)
    matches = sum(r[2] == golds[r[0]].expected_decision for r in rows)
    e2e = [(r[4] - r[3]).total_seconds() for r in rows if r[4] is not None]
    return {
        "schema_version": 1,
        "description": "Load test of the API, queue, workers and database with a SIMULATED model "
        "(eval/load_test.py). No model was called: latency and answers come from a scripted oracle. Not a production "
        "deployment (in-process HTTP, one process). Generated, never edited by hand.",
        "llm": {
            "simulated": True,
            "transport": "priorauth.llm.fake.OracleTransport (replays gold labels; not a model)",
            "latency_s_per_call": latency_s,
            "jitter_s_per_call": jitter_s,
            "seed": seed,
        },
        "machine": machine(),
        "settings": {
            "cases": len(pairs),
            "workers": workers,
            "database": database,
            "api": "in-process ASGI (httpx.ASGITransport): no network, no TLS, one process; bearer token",
            "code_path": "the API's production code: FHIR parsing, queue, workers, model view (minimum necessary + "
            "redaction), verifier, engine, encrypted record fields, audit log",
            "not_production": "simulated model, in-process HTTP, single process; not a deployment",
        },
        "deterministic": {
            "cases": len(rows),
            "statuses": dict(sorted(statuses.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "matches_expected_decision": matches,
            "llm_calls": sum(r[5] for r in rows),
        },
        "timing": {
            "wall_s": round(wall_s, 3),
            "throughput_cases_per_s": round(len(rows) / wall_s, 2),
            "submit_ms": distribution(list(submit_ms)).model_dump(),
            "end_to_end_s": distribution(e2e).model_dump(),
            "peak_concurrent_llm_calls": peak,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=int, default=200)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--latency", type=float, default=0.25)
    parser.add_argument("--jitter", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--database-url", help="default: a fresh SQLite file; any other database is wiped first")
    parser.add_argument("--reset-database", action="store_true", help="required with --database-url: drop its tables")
    parser.add_argument("--expect", type=Path, help="fail unless the deterministic section equals this file's")
    args = parser.parse_args(argv)
    if args.database_url and not args.reset_database:
        parser.error("--database-url drops every table of that database first; confirm with --reset-database")
    report = asyncio.run(
        run(args.cases, args.workers, args.latency, args.jitter, args.seed, args.data, args.database_url)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    d, t = report["deterministic"], report["timing"]
    print(
        f"{d['cases']} cases, {d['statuses']}, {d['matches_expected_decision']} match gold; wall {t['wall_s']} s, "
        f"{t['throughput_cases_per_s']} cases/s, peak {t['peak_concurrent_llm_calls']} concurrent (simulated) "
        f"model calls -> {args.out}"
    )
    if args.expect:
        expected = json.loads(args.expect.read_text(encoding="utf-8"))["deterministic"]
        if expected != d:
            print(f"deterministic section differs from {args.expect}: {expected} != {d}")
            return 1
        print(f"deterministic section matches {args.expect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
