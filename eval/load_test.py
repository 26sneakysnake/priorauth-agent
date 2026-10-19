"""Load test: every synthetic case submitted at once to the API, with a SIMULATED model.

    make load-test        # 200 cases, 32 workers, simulated model latency 0.25 s + up to 0.25 s per call

No model is called. The "model" is priorauth.llm.fake.OracleTransport, a scripted oracle that replays the gold
labels with a seeded, simulated latency: this measures the API, the queue, the workers and the database under
concurrency, not a model. The API runs in-process (httpx.ASGITransport, no network) on a fresh SQLite file.

Output: results/load_test.json. Its `deterministic` section (counts, outcomes, agreement with gold) is identical
from run to run; its `timing` section depends on the machine, which is described in `machine`.
"""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

from eval.metrics import distribution
from eval.run import load_split
from priorauth.agent.loop import AgentSystem
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import CaseRow, JobRow
from priorauth.llm.fake import OracleTransport, fake_client
from priorauth.llm.pricing import HAIKU

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


async def run(n_cases: int, workers: int, latency_s: float, jitter_s: float, seed: int, data: Path) -> dict[str, Any]:
    pairs = [p for split in ("dev", "test", "robust") for p in load_split(data, split)][:n_cases]
    golds = {case.case_id: gold for case, gold in pairs}
    transport = OracleTransport(pairs, latency_s=latency_s, jitter_s=jitter_s, seed=seed)
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

    def system() -> AgentSystem:
        return AgentSystem("agent_v2_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(transport))

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'load.db'}", workers=workers)
        app = create_app(settings, system_factory=system)
        transport_ = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport_, base_url="http://api") as client,
        ):

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
        "description": "Load test of the API, queue and workers with a SIMULATED model (eval/load_test.py). "
        "No model was called: latency and answers come from a scripted oracle. Generated, never edited by hand.",
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
            "database": "SQLite file (aiosqlite)",
            "api": "in-process ASGI (httpx.ASGITransport), no network",
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
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.cases, args.workers, args.latency, args.jitter, args.seed, args.data))
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    d, t = report["deterministic"], report["timing"]
    print(
        f"{d['cases']} cases, {d['statuses']}, {d['matches_expected_decision']} match gold; wall {t['wall_s']} s, "
        f"{t['throughput_cases_per_s']} cases/s, peak {t['peak_concurrent_llm_calls']} concurrent (simulated) "
        f"model calls -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
