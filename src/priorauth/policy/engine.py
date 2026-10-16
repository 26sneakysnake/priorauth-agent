"""Deterministic policy engine: the only component that turns findings into a decision.

The engine approves a request only when every criterion of the procedure has a MET finding whose evidence was
verified against the record, nothing conflicts and no prompt injection was detected. Every other situation, including
malformed input, routes the case to a human reviewer (PEND) with the reasons. There is no adverse outcome: the engine
never denies a request.
"""

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from priorauth.policy.criteria import PROCEDURES
from priorauth.policy.models import CaseFindings, Finding, Status


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    PEND = "PEND"


class ReasonCode(StrEnum):
    UNKNOWN_PROCEDURE = "UNKNOWN_PROCEDURE"
    UNKNOWN_CRITERION = "UNKNOWN_CRITERION"
    MISSING_FINDING = "MISSING_FINDING"
    DUPLICATE_DISAGREES = "DUPLICATE_DISAGREES"
    NOT_MET = "NOT_MET"
    INSUFFICIENT = "INSUFFICIENT"
    NO_EVIDENCE = "NO_EVIDENCE"
    UNVERIFIED_EVIDENCE = "UNVERIFIED_EVIDENCE"
    CONFLICT = "CONFLICT"
    INJECTION_DETECTED = "INJECTION_DETECTED"


class Reason(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ReasonCode
    criterion_id: str | None = None
    detail: str = ""


class Decision(BaseModel):
    """`criteria` is the status the engine retained for each policy criterion (INSUFFICIENT when unusable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    reasons: tuple[Reason, ...]
    criteria: dict[str, Status]


def _retained_status(cid: str, findings: list[Finding], reasons: list[Reason]) -> Status:
    """Status the engine keeps for one criterion, appending the reasons that block approval."""
    if not findings:
        reasons.append(Reason(code=ReasonCode.MISSING_FINDING, criterion_id=cid, detail="no finding for criterion"))
        return Status.INSUFFICIENT
    statuses = {f.status for f in findings}
    if len(statuses) > 1:
        detail = "findings disagree: " + ", ".join(sorted(statuses))
        reasons.append(Reason(code=ReasonCode.DUPLICATE_DISAGREES, criterion_id=cid, detail=detail))
        return Status.INSUFFICIENT
    status = statuses.pop()
    if any(f.conflict for f in findings):
        reasons.append(Reason(code=ReasonCode.CONFLICT, criterion_id=cid, detail="record contradicts itself"))
        return Status.INSUFFICIENT
    if status is Status.NOT_MET:
        reasons.append(Reason(code=ReasonCode.NOT_MET, criterion_id=cid, detail=_notes(findings)))
        return status
    if status is Status.INSUFFICIENT:
        reasons.append(Reason(code=ReasonCode.INSUFFICIENT, criterion_id=cid, detail=_notes(findings)))
        return status
    # MET: only usable with evidence, all of it verified against the record.
    if not any(f.evidence for f in findings):
        reasons.append(Reason(code=ReasonCode.NO_EVIDENCE, criterion_id=cid, detail="MET without a citation"))
        return Status.INSUFFICIENT
    if not all(f.verified for f in findings):
        reasons.append(
            Reason(code=ReasonCode.UNVERIFIED_EVIDENCE, criterion_id=cid, detail="citation not found in the record")
        )
        return Status.INSUFFICIENT
    return Status.MET


def _notes(findings: list[Finding]) -> str:
    return "; ".join(f.note for f in findings if f.note)


def decide(case: CaseFindings) -> Decision:
    procedure = PROCEDURES.get(case.procedure_code)
    reasons: list[Reason] = []
    if procedure is None:
        reasons.append(Reason(code=ReasonCode.UNKNOWN_PROCEDURE, detail=case.procedure_code))
        return Decision(outcome=Outcome.PEND, reasons=tuple(reasons), criteria={})

    by_criterion: defaultdict[str, list[Finding]] = defaultdict(list)
    for f in case.findings:
        by_criterion[f.criterion_id].append(f)
    for cid in sorted(set(by_criterion) - set(procedure.criterion_ids)):
        reasons.append(Reason(code=ReasonCode.UNKNOWN_CRITERION, criterion_id=cid, detail="not in this policy"))

    criteria = {cid: _retained_status(cid, by_criterion.get(cid, []), reasons) for cid in procedure.criterion_ids}

    if case.injection_detected:
        reasons.append(Reason(code=ReasonCode.INJECTION_DETECTED, detail="instructions aimed at the reviewer"))

    outcome = Outcome.APPROVE if not reasons else Outcome.PEND
    return Decision(outcome=outcome, reasons=tuple(reasons), criteria=criteria)
