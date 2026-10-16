"""Run one system on one split; write the metrics and the error log.

    python -m eval.run --system rules_baseline --split dev

Outputs (never edited by hand; the README is generated from them):
    results/<system>_<split>.json          metrics with 95 % Wilson intervals
    results/<system>_<split>_errors.jsonl  one line per case with a wrong decision, a wrong criterion status,
                                           an invalid citation or a system error

Both files are deterministic for a deterministic system (same code + same data -> same bytes).
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from eval.metrics import compute
from eval.systems import get_system
from priorauth.data.integrity import digest_dir
from priorauth.data.schema import Case, Gold
from priorauth.pipeline import CaseRun, System, run_case
from priorauth.policy.models import Status

SCHEMA_VERSION = 1


def load_split(data_root: Path, split: str) -> list[tuple[Case, Gold]]:
    pairs = []
    for path in sorted((data_root / split / "cases").glob("*.json")):
        case = Case.model_validate_json(path.read_bytes())
        gold = Gold.model_validate_json((data_root / split / "gold" / path.name).read_bytes())
        pairs.append((case, gold))
    if not pairs:
        raise SystemExit(f"no cases under {data_root / split}")
    return pairs


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
                f.model_dump(mode="json", include={"status", "evidence", "note"})
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


def evaluate(system_name: str, split: str, data_root: Path, concurrency: int = 8) -> tuple[bytes, bytes]:
    """Return (results JSON, errors JSONL) as bytes."""
    pairs = load_split(data_root, split)
    system = get_system(system_name)
    runs = asyncio.run(run_all(system, [c for c, _ in pairs], concurrency))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--system", required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args(argv)

    results, errors = evaluate(args.system, args.split, args.data, args.concurrency)
    args.out.mkdir(parents=True, exist_ok=True)
    results_path, errors_path = output_paths(args.out, args.system, args.split)
    results_path.write_bytes(results)
    errors_path.write_bytes(errors)
    m = json.loads(results)["metrics"]
    da, acc = m["dangerous_approval_rate"], m["decision_accuracy"]
    print(
        f"{args.system} on {args.split}: dangerous approvals {da['k']}/{da['n']}, "
        f"decision accuracy {acc['k']}/{acc['n']} -> {results_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
