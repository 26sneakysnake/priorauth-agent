"""Agent v2 on Haiku through the API's whole pipeline, measured with a real model (P7bis).

    LLM_MODE=record uv run python -m eval.pipeline --split dev --max-cost-usd 0.45   # calls the API, records
    uv run python -m eval.pipeline --split dev                                       # replays the cassettes

Every case of the split goes the way a submission goes in production code: FHIR Bundle -> POST /cases (bearer token)
-> job queue -> worker -> model view (minimum necessary pages, redacted identifiers) -> agent v2 on Haiku ->
citations restored and verified on the original pages -> policy engine -> encrypted result. The API runs in-process
(httpx.ASGITransport) on a fresh SQLite file; that is the one part that differs from a deployment (Postgres, network).

The comparison point is results/agent_v2_haiku_<split>.json: the same agent and prompt reading whole, unredacted
records through the evaluation harness. Nothing was tuned after looking at test or robust.

Outputs (never edited by hand): results/pipeline_<split>.json (the metrics of results/<system>_<split>.json plus
the pages sent) and results/pipeline_<split>_errors.jsonl. Cassettes and ledger scope: agent_v2_haiku_pipeline/<split>.
Replaying is deterministic: same code, data and cassettes give the same bytes. In record mode the run first
estimates its cost and refuses to start above --max-cost-usd; a run or phase cap hit during the run writes nothing.
"""

import argparse
import asyncio
import json
import os
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

from eval.budget import estimate_run_cost
from eval.metrics import compute
from eval.run import ABORTING_ERRORS, SPLITS, RunAborted, error_record, load_split
from eval.systems import HAIKU_CASE_CAP_USD
from priorauth.agent.loop import AgentSystem
from priorauth.agent.verifier import VerificationReport
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.data.integrity import digest_dir
from priorauth.data.schema import Case, Gold
from priorauth.jobs.db import CaseRow, JobRow
from priorauth.llm.client import LLMClient, LLMMode
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import CaseRun, System, Usage
from priorauth.policy.engine import decide
from priorauth.policy.models import CaseFindings
from priorauth.security.crypto import ENV as FIELD_KEY_ENV
from priorauth.security.crypto import new_key
from priorauth.security.model_view import ProtectedSystem, model_view

SYSTEM_NAME = "agent_v2_haiku_pipeline"
BASELINE = "agent_v2_haiku"  # same agent and prompt on whole records (P3)
SCHEMA_VERSION = 1


def pipeline_system(client: LLMClient | None) -> ProtectedSystem:
    """The API's production system (priorauth.api.app.default_system) with an explicit client."""
    return ProtectedSystem(AgentSystem(BASELINE, HAIKU, "agent@v2", HAIKU_CASE_CAP_USD, llm=client), name=SYSTEM_NAME)


async def through_api(
    pairs: list[tuple[Case, Gold]], system_factory: Callable[[], System], workers: int
) -> list[CaseRun]:
    """Submit every case to the in-process API and return the stored CaseRun of each, in the order of `pairs`."""
    os.environ.setdefault(FIELD_KEY_ENV, new_key())
    password = secrets.token_urlsafe(16)
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{Path(tmp) / 'pipeline.db'}",
            workers=workers,
            jwt_secret=secrets.token_urlsafe(32),
            audit_key=secrets.token_urlsafe(32),
            demo_password=password,
            review_feedback_path=None,
            case_cost_cap_usd=HAIKU_CASE_CAP_USD,
        )
        app = create_app(settings, system_factory=system_factory)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client,
        ):
            r = await client.post("/auth/token", json={"username": "demo-system", "password": password})
            r.raise_for_status()
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            for case, _ in pairs:
                r = await client.post(
                    "/cases", json=case_to_bundle(case, case.case_id), headers={"Idempotency-Key": case.case_id}
                )
                r.raise_for_status()
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
                await asyncio.sleep(0.1)
            async with sessions() as s:
                rows = (
                    await s.execute(
                        select(
                            CaseRow.submitter_case_id, CaseRow.procedure_code, JobRow.result_json, JobRow.error
                        ).join(JobRow, JobRow.case_id == CaseRow.id)
                    )
                ).all()
    runs: dict[str, CaseRun] = {}
    for submitter_id, procedure_code, result_json, error in rows:
        if result_json:
            runs[submitter_id] = CaseRun.model_validate_json(result_json)
        else:  # failed after every attempt: the fail-closed decision the API serves
            empty = CaseFindings(procedure_code=procedure_code, findings=())
            runs[submitter_id] = CaseRun(
                case_id=submitter_id,
                raw=empty,
                verified=empty,
                verification=VerificationReport(n_quotes=0, n_valid=0, invalid=(), downgraded=()),
                decision=decide(empty),
                usage=Usage(),
                error=f"{error}: job failed",
            )
    return [runs[case.case_id] for case, _ in pairs]


def pages_sent(pairs: list[tuple[Case, Gold]]) -> dict[str, Any]:
    pages = sent = 0
    for case, _ in pairs:
        view = model_view(case)
        pages += len(case.pages)
        sent += len(view.page_map)
    return {"pages": pages, "pages_sent": sent, "share_of_pages_sent": round(sent / pages, 4)}


def evaluate(
    split: str, data: Path, client: LLMClient, workers: int = 8, max_cost_usd: float | None = None
) -> tuple[bytes, bytes]:
    pairs = load_split(data, split)
    client.begin_run(f"{SYSTEM_NAME}/{split}", max_cost_usd)
    try:
        runs = asyncio.run(through_api(pairs, lambda: pipeline_system(client), workers))
    finally:
        client.end_run()
    aborted = sorted({r.error for r in runs if r.error and r.error.startswith(ABORTING_ERRORS)})
    if aborted:
        raise RunAborted(f"{SYSTEM_NAME} on {split} stopped by a cost cap: {aborted[0]}")
    golds = [g for _, g in pairs]
    metrics = compute(list(zip(golds, runs, strict=True)))
    results = {
        "schema_version": SCHEMA_VERSION,
        "system": SYSTEM_NAME,
        "split": split,
        "data_digest": digest_dir(data / split),
        "description": "agent@v2 on claude-haiku-5-5 through the API pipeline: FHIR Bundle, queue, worker, model "
        "view (minimum necessary pages, redacted identifiers), verifier, engine (eval/pipeline.py). Real model "
        "responses, recorded in eval/cassettes/agent_v2_haiku_pipeline/. Generated, never edited by hand.",
        "pipeline": pages_sent(pairs),
        "metrics": metrics.model_dump(mode="json"),
    }
    errors = [rec for g, r in zip(golds, runs, strict=True) if (rec := error_record(g, r)) is not None]
    return (
        (json.dumps(results, indent=2, ensure_ascii=False) + "\n").encode(),
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in errors).encode(),
    )


def preflight(split: str, data: Path, client: LLMClient, cap: float | None) -> None:
    if client.mode is LLMMode.REPLAY or client.ledger is None:
        return
    cases = [c for c, _ in load_split(data, split)]
    agent = pipeline_system(client).inner
    if not isinstance(agent, AgentSystem):  # always an AgentSystem; keeps the type checker informed
        raise RunAborted("no cost estimate for this system")
    estimate, source = estimate_run_cost(SYSTEM_NAME, agent, cases, client.cassettes, BASELINE)
    remaining = client.ledger.remaining_usd
    print(
        f"{SYSTEM_NAME} on {split} ({len(cases)} cases, {client.mode} mode): estimated up to {estimate:.4f} USD "
        f"({source}); ledger cap left {remaining:.4f} USD; run cap {cap if cap is not None else 'none'}"
    )
    if estimate > remaining or (cap is not None and estimate > cap):
        raise RunAborted("estimated cost exceeds the remaining budget or the run cap; not starting")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--split", choices=SPLITS, required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-cost-usd", type=float, help="hard cap on real API spend for this run")
    args = parser.parse_args(argv)
    client = LLMClient.from_env()
    if client.mode is not LLMMode.REPLAY and args.max_cost_usd is None:
        parser.error("a paid run needs --max-cost-usd")
    try:
        preflight(args.split, args.data, client, args.max_cost_usd)
        results, errors = evaluate(args.split, args.data, client, args.workers, args.max_cost_usd)
    except RunAborted as e:
        print(f"aborted: {e}")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"pipeline_{args.split}.json").write_bytes(results)
    (args.out / f"pipeline_{args.split}_errors.jsonl").write_bytes(errors)
    m = json.loads(results)["metrics"]
    print(
        f"{SYSTEM_NAME} on {args.split}: dangerous approvals {m['dangerous_approval_rate']['k']}/"
        f"{m['dangerous_approval_rate']['n']}, decisions {m['decision_accuracy']['k']}/{m['decision_accuracy']['n']}, "
        f"criteria {m['criterion_accuracy']['k']}/{m['criterion_accuracy']['n']}, "
        f"cost {m['usage']['total_cost_usd']} USD -> {args.out / f'pipeline_{args.split}.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
