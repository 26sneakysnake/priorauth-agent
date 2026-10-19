"""Run one system on one split; write the metrics and the error log.

    python -m eval.run --system rules_baseline --split dev
    LLM_MODE=record python -m eval.run --system agent_v1_haiku --split dev --max-cost-usd 1.0
    python -m eval.run --offline-all            # replay every committed result (no network, no API key)

Outputs (never edited by hand; the README is generated from them):
    results/<system>_<split>.json          metrics with 95 % Wilson intervals
    results/<system>_<split>_errors.jsonl  one line per case with a wrong decision, a wrong criterion status,
                                           an invalid citation or a system error

Both files are deterministic for a deterministic system and for an LLM system replayed from its cassettes
(same code + same data + same cassettes -> same bytes): latency and cost are read from the cassettes.

In record and live modes the run first estimates its cost (eval.budget) and refuses to start if the estimate does
not fit the remaining phase budget or --max-cost-usd. If a run or phase cap is hit during the run, nothing is
written to results/ (the cassettes and the spend ledger keep what was paid for) and the exit code is 2.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from eval.budget import Estimable, estimate_run_cost
from eval.metrics import compute
from eval.systems import COST_PRIOR, OFFLINE, get_system
from priorauth.data.integrity import digest_dir
from priorauth.data.schema import Case, Gold
from priorauth.llm.client import LLMClient, LLMMode
from priorauth.pipeline import CaseRun, System, run_case
from priorauth.policy.models import Status

SCHEMA_VERSION = 1
SPLITS = ("dev", "test", "robust")
ABORTING_ERRORS = ("BudgetExceeded: run budget", "BudgetExceeded: phase budget")


class RunAborted(RuntimeError):
    pass


def load_split(data_root: Path, split: str) -> list[tuple[Case, Gold]]:
    pairs = []
    for path in sorted((data_root / split / "cases").glob("*.json")):
        case = Case.model_validate_json(path.read_bytes())
        gold = Gold.model_validate_json((data_root / split / "gold" / path.name).read_bytes())
        pairs.append((case, gold))
    if not pairs:
        raise SystemExit(f"no cases under {data_root / split}")
    return pairs


def llm_client(system: System) -> LLMClient | None:
    client = getattr(system, "client", None)
    return client if isinstance(client, LLMClient) else None


async def run_all(system: System, cases: list[Case], concurrency: int) -> list[CaseRun]:
    sem = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> CaseRun:
        async with sem:
            return await run_case(system, case)

    return list(await asyncio.gather(*(one(c) for c in cases)))


def error_record(gold: Gold, run: CaseRun) -> dict[str, Any] | None:
    predicted = run.decision.outcome.value
    mismatches = []
    for cid, gc in gold.criteria.items():
        retained = run.decision.criteria.get(cid, Status.INSUFFICIENT)
        if retained is not gc.status:
            claimed = [
                f.model_dump(mode="json", include={"status", "evidence", "conflict", "note"})
                for f in run.raw.findings
                if f.criterion_id == cid
            ]
            mismatches.append(
                {
                    "criterion_id": cid,
                    "gold": gc.status.value,
                    "retained": retained.value,
                    "gold_rationale": gc.rationale,
                    "system": claimed,
                }
            )
    if predicted == gold.expected_decision and not mismatches and not run.verification.invalid and not run.error:
        return None
    return {
        "case_id": gold.case_id,
        "procedure_code": gold.procedure_code,
        "trap": gold.trap.value,
        "target_criterion": gold.target_criterion,
        "expected": gold.expected_decision,
        "predicted": predicted,
        "dangerous_approval": gold.expected_decision == "PEND" and predicted == "APPROVE",
        "injection": gold.injection,
        "injection_detected": run.raw.injection_detected,
        "reasons": [r.model_dump(mode="json") for r in run.decision.reasons],
        "criteria": mismatches,
        "invalid_evidence": [i.model_dump(mode="json") for i in run.verification.invalid],
        "error": run.error,
    }


def evaluate(
    system_name: str,
    split: str,
    data_root: Path,
    concurrency: int = 8,
    *,
    case_ids: frozenset[str] | None = None,
    max_cost_usd: float | None = None,
    scope: str | None = None,
) -> tuple[bytes, bytes]:
    """Return (results JSON, errors JSONL) as bytes. `case_ids` restricts the run to a subset (debugging only)."""
    pairs = load_split(data_root, split)
    if case_ids is not None:
        pairs = [(c, g) for c, g in pairs if c.case_id in case_ids]
    system = get_system(system_name)
    client = llm_client(system)
    if client is not None:
        client.begin_run(scope or f"{system_name}/{split}", max_cost_usd)
    try:
        runs = asyncio.run(run_all(system, [c for c, _ in pairs], concurrency))
    finally:
        if client is not None:
            client.end_run()
    aborted = sorted({r.error for r in runs if r.error and r.error.startswith(ABORTING_ERRORS)})
    if aborted:
        raise RunAborted(f"{system_name} on {split} stopped by a cost cap: {aborted[0]}")
    golds = [g for _, g in pairs]
    metrics = compute(list(zip(golds, runs, strict=True)))
    results = {
        "schema_version": SCHEMA_VERSION,
        "system": system_name,
        "split": split,
        "data_digest": digest_dir(data_root / split),
        "metrics": metrics.model_dump(mode="json"),
    }
    errors = [rec for g, r in zip(golds, runs, strict=True) if (rec := error_record(g, r)) is not None]
    results_bytes = (json.dumps(results, indent=2, ensure_ascii=False) + "\n").encode()
    errors_bytes = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in errors).encode()
    return results_bytes, errors_bytes


def output_paths(out: Path, system_name: str, split: str) -> tuple[Path, Path]:
    return out / f"{system_name}_{split}.json", out / f"{system_name}_{split}_errors.jsonl"


def preflight(
    system_name: str, split: str, data_root: Path, case_ids: frozenset[str] | None, cap: float | None
) -> None:
    """Refuse a paid run whose estimated cost does not fit the remaining budget."""
    system = get_system(system_name)
    client = llm_client(system)
    if client is None or client.mode is LLMMode.REPLAY or client.ledger is None:
        return
    cases = [c for c, _ in load_split(data_root, split) if case_ids is None or c.case_id in case_ids]
    if not isinstance(system, Estimable):
        raise RunAborted(f"{system_name} has no cost estimate; refusing a paid run")
    estimate, source = estimate_run_cost(system_name, system, cases, client.cassettes, COST_PRIOR.get(system_name))
    remaining = client.ledger.remaining_usd
    print(
        f"{system_name} on {split} ({len(cases)} cases, {client.mode} mode): estimated up to {estimate:.4f} USD "
        f"({source}); phase budget left {remaining:.4f} USD; run cap {cap if cap is not None else 'none'}"
    )
    if estimate > remaining or (cap is not None and estimate > cap):
        raise RunAborted("estimated cost exceeds the remaining budget or the run cap; not starting")


def write_outputs(out: Path, system_name: str, split: str, results: bytes, errors: bytes) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    results_path, errors_path = output_paths(out, system_name, split)
    results_path.write_bytes(results)
    errors_path.write_bytes(errors)
    m = json.loads(results)["metrics"]
    da, acc = m["dangerous_approval_rate"], m["decision_accuracy"]
    print(
        f"{system_name} on {split}: dangerous approvals {da['k']}/{da['n']}, "
        f"decision accuracy {acc['k']}/{acc['n']}, cost {m['usage']['total_cost_usd']} USD -> {results_path}"
    )
    return results_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--system")
    parser.add_argument("--split", choices=SPLITS)
    parser.add_argument("--offline-all", action="store_true", help="replay every system/split listed in OFFLINE")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--cases", help="comma-separated case ids (debugging; use with --out outside results/)")
    parser.add_argument("--max-cost-usd", type=float, help="hard cap on real API spend for this run")
    parser.add_argument("--scope", help="cassette/ledger scope (default <system>/<split>)")
    args = parser.parse_args(argv)

    if args.offline_all:
        for system_name, splits in OFFLINE.items():
            for split in splits:
                results, errors = evaluate(system_name, split, args.data, args.concurrency)
                write_outputs(args.out, system_name, split, results, errors)
        return 0
    if not args.system or not args.split:
        parser.error("--system and --split are required (or --offline-all)")
    case_ids = frozenset(args.cases.split(",")) if args.cases else None
    if case_ids is not None and args.out.resolve() == Path("results").resolve():
        parser.error("a subset run must not overwrite results/; pass --out")
    try:
        preflight(args.system, args.split, args.data, case_ids, args.max_cost_usd)
        results, errors = evaluate(
            args.system,
            args.split,
            args.data,
            args.concurrency,
            case_ids=case_ids,
            max_cost_usd=args.max_cost_usd,
            scope=args.scope,
        )
    except RunAborted as e:
        print(f"aborted: {e}", file=sys.stderr)
        return 2
    write_outputs(args.out, args.system, args.split, results, errors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
