"""Turn reviewer feedback into dev evaluation cases.

    uv run python -m eval.review_to_cases --database-url sqlite+aiosqlite:///./priorauth.db
        # reads eval/cases_from_review.jsonl, writes data/dev_review/{cases,gold}/<id>.json

Each line of the feedback file (written by POST /cases/{id}/reviews) names a case, an action and, for a flagged
error, a criterion and its corrected status. For every reviewed case, the record is read back from the database
(decrypted with PRIORAUTH_FIELD_KEY) and a gold label is built:
- start from the status the engine retained for each criterion, with the agent's verified evidence;
- an `approve` review sets every criterion to MET (the reviewer judged the case approvable);
- each `flag_error` review then sets its criterion to the corrected status (the latest one wins); a corrected
  criterion keeps the agent's evidence only if its status did not change;
- `request_info` reviews give no label: a case with only those is skipped.
expected_decision is APPROVE only if every criterion is MET and the agent saw no injection.

The output is a separate dev directory: data/dev, data/test and data/robust are never touched, and data/test is
never fed back (rule 6 of CLAUDE.md). Evaluate with eval.run.load_split(Path("data"), "dev_review").
"""

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select

from priorauth.data.schema import Case, Gold, GoldCriterion, Trap
from priorauth.jobs.db import CaseRow, JobRow, make_engine, sessions
from priorauth.pipeline import CaseRun
from priorauth.policy.criteria import PROCEDURES
from priorauth.policy.models import Status

FEEDBACK = Path("eval/cases_from_review.jsonl")
OUTPUT = Path("data/dev_review")


def read_feedback(path: Path) -> dict[str, list[dict[str, Any]]]:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            by_case[entry["case_id"]].append(entry)
    for entries in by_case.values():
        entries.sort(key=lambda e: e["reviewed_at"])
    return dict(by_case)


def gold_from_review(case: Case, run: CaseRun | None, entries: list[dict[str, Any]]) -> Gold | None:
    procedure = PROCEDURES[case.procedure_code]
    labelled = [e for e in entries if e["action"] in ("approve", "flag_error")]
    if not labelled:
        return None
    findings = {f.criterion_id: f for f in run.verified.findings} if run else {}
    retained = run.decision.criteria if run else {}
    statuses = {cid: retained.get(cid, Status.INSUFFICIENT) for cid in procedure.criterion_ids}
    if any(e["action"] == "approve" for e in entries):
        statuses = dict.fromkeys(statuses, Status.MET)
    corrected: list[str] = []
    for e in entries:
        if e["action"] == "flag_error" and e["criterion_id"] in statuses:
            statuses[e["criterion_id"]] = Status(e["corrected_status"])
            corrected.append(e["criterion_id"])
    criteria = {}
    for cid, status in statuses.items():
        f = findings.get(cid)
        keep = f is not None and f.verified and f.status == status
        criteria[cid] = GoldCriterion(
            status=status,
            evidence=f.evidence if keep and f else (),
            rationale="labelled by a human reviewer" + (" (corrected)" if cid in corrected else ""),
        )
    injection = bool(run and run.verified.injection_detected)
    approve = all(s == Status.MET for s in statuses.values()) and not injection
    return Gold(
        case_id=case.case_id,
        procedure_code=case.procedure_code,
        trap=Trap.FROM_REVIEW,
        target_criterion=corrected[0] if corrected else None,
        expected_decision="APPROVE" if approve else "PEND",
        criteria=criteria,
        injection=injection,
        injection_evidence=run.verified.injection_evidence if run and injection else (),
        paraphrased=(),
        page_kinds=tuple("unknown" for _ in case.pages),
    )


async def convert(database_url: str, feedback: Path, out: Path) -> dict[str, int]:
    by_case = read_feedback(feedback)
    engine = make_engine(database_url)
    written = skipped = 0
    try:
        async with sessions(engine)() as s:
            rows = (
                await s.execute(
                    select(CaseRow, JobRow).join(JobRow, JobRow.case_id == CaseRow.id).where(CaseRow.id.in_(by_case))
                )
            ).all()
    finally:
        await engine.dispose()
    (out / "cases").mkdir(parents=True, exist_ok=True)
    (out / "gold").mkdir(parents=True, exist_ok=True)
    for row, job in sorted(rows, key=lambda r: r[0].created_at):
        stored = Case.model_validate_json(row.case_json)
        case = stored.model_copy(update={"case_id": f"review-{row.id}"})
        run = CaseRun.model_validate_json(job.result_json) if job.result_json else None
        gold = gold_from_review(case, run, by_case[row.id])
        if gold is None:
            skipped += 1
            continue
        name = f"{case.case_id}.json"
        (out / "cases" / name).write_text(case.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (out / "gold" / name).write_text(gold.model_dump_json(indent=2) + "\n", encoding="utf-8")
        written += 1
    return {"reviewed_cases": len(by_case), "written": written, "skipped_no_label": skipped,
            "not_in_database": len(by_case) - len(rows)}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--feedback", type=Path, default=FEEDBACK)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if args.out.resolve().name in ("dev", "test", "robust"):
        parser.error("the generated splits are never overwritten; write to a separate directory")
    print(json.dumps(asyncio.run(convert(args.database_url, args.feedback, args.out))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
