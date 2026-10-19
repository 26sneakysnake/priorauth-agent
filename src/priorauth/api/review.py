"""Human review of pended cases: the queue, one case with its citations located in the pages, and review actions.

    GET  /review/queue            PEND cases that no reviewer has approved yet: reasons, age, procedure filter
    GET  /cases/{id}/review       the original pages; every criterion with its evidence and exact offsets
    POST /cases/{id}/reviews      approve | request_info (reason) | flag_error (criterion, corrected status, comment)

Roles: reviewer and admin. Offsets come from priorauth.agent.verifier.locate, i.e. the same normalization that
verified the quote, so a highlight shows exactly the characters a quote was checked against.

A reviewer's approval becomes the case's final decision: GET /cases/{id} and the ClaimResponse say APPROVE (certified,
A1, decided by the reviewer) and a signed `case.reviewed` webhook is queued in the same transaction as the review
(priorauth.jobs.queue.JobQueue.record_review). The engine's decision is kept beside it: the stored CaseRun, the job's
engine outcome and the ClaimResponse's process notes still say PEND with its reasons. After an approval the case is
closed to further reviews (409). Every review is audited and appended as one line of identifiers and labels (never
the comment) to the feedback file, PRIORAUTH_REVIEW_FEEDBACK_PATH (default eval/cases_from_review.jsonl);
eval/review_to_cases.py turns those lines into dev evaluation cases.
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from priorauth.agent.verifier import locate
from priorauth.api.deps import REVIEWER, UUID_SHAPE
from priorauth.data.schema import Case
from priorauth.jobs.db import CaseRow, JobRow, ReviewRow, utcnow
from priorauth.pipeline import CaseRun
from priorauth.policy.criteria import PROCEDURES
from priorauth.policy.engine import Decision, Outcome, decide
from priorauth.policy.models import CaseFindings, Evidence, Status
from priorauth.security.audit import AuditLog
from priorauth.security.auth import Principal

DEFAULT_FEEDBACK_PATH = "eval/cases_from_review.jsonl"
Action = Literal["approve", "request_info", "flag_error"]

router = APIRouter(tags=["review"])


class ReasonView(BaseModel):
    code: str
    criterion_id: str | None
    detail: str


class QueueItem(BaseModel):
    case_id: str
    submitter_case_id: str | None
    procedure_code: str
    created_at: datetime
    finished_at: datetime | None
    age_seconds: int
    reasons: list[ReasonView]
    review_status: Action | None  # the latest review, if any


class EvidenceView(BaseModel):
    page: int
    quote: str
    valid: bool  # accepted by the verifier
    start: int | None  # [start, end) in the cited page's text; None when not located
    end: int | None


class CriterionView(BaseModel):
    criterion_id: str
    title: str
    requirement: str
    status: Status  # what the engine retained
    claimed_status: Status | None  # what the agent recorded, before verification
    verified: bool
    conflict: bool
    note: str
    evidence: list[EvidenceView]


class PageView(BaseModel):
    number: int
    text: str


class ReviewView(BaseModel):
    id: str
    action: Action
    criterion_id: str | None
    corrected_status: Status | None
    comment: str | None
    reviewer_id: str
    created_at: datetime


class CaseReview(BaseModel):
    case_id: str
    submitter_case_id: str | None
    procedure_code: str
    procedure_name: str
    created_at: datetime
    job_status: str
    outcome: str | None  # the engine's decision
    reviewer_outcome: str | None  # APPROVE once a reviewer approved the case
    extracted_by: str | None
    error: str | None
    pages: list[PageView]
    criteria: list[CriterionView]
    reasons: list[ReasonView]
    injection_detected: bool
    injection_evidence: list[EvidenceView]
    reviews: list[ReviewView]


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    reason: str | None = Field(default=None, max_length=2000, description="request_info: what is missing")
    criterion_id: str | None = Field(default=None, description="flag_error: the criterion the agent got wrong")
    corrected_status: Status | None = Field(default=None, description="flag_error: the right status")
    comment: str | None = Field(default=None, max_length=2000, description="flag_error: why (approve: optional)")

    @model_validator(mode="after")
    def _fields_for_action(self) -> "ReviewIn":
        if self.action == "request_info" and not (self.reason or "").strip():
            raise ValueError("request_info needs a reason")
        complete = bool(self.criterion_id) and self.corrected_status is not None and bool((self.comment or "").strip())
        if self.action == "flag_error" and not complete:
            raise ValueError("flag_error needs criterion_id, corrected_status and comment")
        if self.action != "flag_error" and (self.criterion_id or self.corrected_status):
            raise ValueError("criterion_id and corrected_status belong to flag_error only")
        return self


def _decision(job: JobRow, case: CaseRow) -> tuple[Decision, CaseRun | None]:
    if job.status == "done" and job.result_json:
        run = CaseRun.model_validate_json(job.result_json)
        return run.decision, run
    return decide(CaseFindings(procedure_code=case.procedure_code, findings=())), None  # failed: fail-closed PEND


def _reasons(decision: Decision) -> list[ReasonView]:
    return [ReasonView(code=r.code.value, criterion_id=r.criterion_id, detail=r.detail) for r in decision.reasons]


def _evidence(case: Case, ev: Evidence, valid: bool) -> EvidenceView:
    span = locate(case.page(ev.page).text, ev.quote) if 1 <= ev.page <= len(case.pages) else None
    start, end = span if span else (None, None)
    return EvidenceView(page=ev.page, quote=ev.quote, valid=valid, start=start, end=end)


class FeedbackSink:
    """Appends one JSON line per review; lines hold identifiers and labels, never free text."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = asyncio.Lock()

    async def write(self, line: dict[str, object]) -> None:
        if self.path is None:
            return
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")


async def _load(request: Request, case_id: str) -> tuple[CaseRow, JobRow, list[ReviewRow]]:
    async with request.app.state.sessions() as s:
        row = (
            await s.execute(
                select(CaseRow, JobRow).join(JobRow, JobRow.case_id == CaseRow.id).where(CaseRow.id == case_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown case")
        reviews = (
            await s.execute(select(ReviewRow).where(ReviewRow.case_id == case_id).order_by(ReviewRow.created_at))
        ).scalars()
        return row[0], row[1], list(reviews)


def _audit(request: Request) -> AuditLog:
    audit: AuditLog = request.app.state.audit
    return audit


@router.get("/review/queue", response_model=list[QueueItem], summary="Pended cases waiting for a reviewer")
async def queue(
    request: Request,
    principal: Annotated[Principal, REVIEWER],
    procedure: str | None = None,
    limit: int = 200,
) -> list[QueueItem]:
    now = utcnow()
    async with request.app.state.sessions() as s:
        stmt = (
            select(CaseRow, JobRow)
            .join(JobRow, JobRow.case_id == CaseRow.id)
            .where(JobRow.status.in_(("done", "failed")), JobRow.outcome == Outcome.PEND.value)
            .order_by(CaseRow.created_at)
        )
        if procedure:
            stmt = stmt.where(CaseRow.procedure_code == procedure)
        rows = (await s.execute(stmt)).all()
        latest: dict[str, str] = {}
        for case_id, action in (
            await s.execute(select(ReviewRow.case_id, ReviewRow.action).order_by(ReviewRow.created_at))
        ).all():
            latest[case_id] = action
    items = []
    for case, job in rows:
        if latest.get(case.id) == "approve":
            continue
        decision, _ = _decision(job, case)
        items.append(
            QueueItem(
                case_id=case.id,
                submitter_case_id=case.submitter_case_id,
                procedure_code=case.procedure_code,
                created_at=case.created_at,
                finished_at=job.finished_at,
                age_seconds=int((now - case.created_at).total_seconds()),
                reasons=_reasons(decision),
                review_status=latest.get(case.id),  # type: ignore[arg-type]  # stored actions are Action values
            )
        )
    items = items[: max(1, min(limit, 1000))]
    await _audit(request).record(principal.user_id, principal.role, "review.queue", "/review/queue", entries=len(items))
    return items


@router.get(
    "/cases/{case_id}/review",
    response_model=CaseReview,
    responses={404: {"description": "unknown case"}},
    summary="One case for review: pages, criteria, evidence with offsets",
)
async def case_review(request: Request, case_id: str, principal: Annotated[Principal, REVIEWER]) -> CaseReview:
    audit = _audit(request)
    try:
        row, job, reviews = await _load(request, case_id)
    except HTTPException:
        probed = case_id if UUID_SHAPE.fullmatch(case_id) else "malformed-id"
        await audit.record(principal.user_id, principal.role, "review.read", probed, status=404)
        raise
    case = Case.model_validate_json(row.case_json)
    procedure = PROCEDURES.get(row.procedure_code)
    decision, run = _decision(job, row)
    findings = {f.criterion_id: f for f in run.verified.findings} if run else {}
    claimed = {f.criterion_id: f.status for f in run.raw.findings} if run else {}
    invalid = {(i.criterion_id, i.evidence) for i in run.verification.invalid} if run else set()
    criteria = []
    for c in procedure.criteria if procedure else ():
        f = findings.get(c.id)
        criteria.append(
            CriterionView(
                criterion_id=c.id,
                title=c.title,
                requirement=c.requirement,
                status=decision.criteria.get(c.id, Status.INSUFFICIENT),
                claimed_status=claimed.get(c.id),
                verified=bool(f and f.verified),
                conflict=bool(f and f.conflict),
                note=f.note if f else "",
                evidence=[_evidence(case, ev, (c.id, ev) not in invalid) for ev in (f.evidence if f else ())],
            )
        )
    injection = run.verified.injection_evidence if run else ()
    await audit.record(principal.user_id, principal.role, "review.read", case_id, status=200)
    return CaseReview(
        case_id=row.id,
        submitter_case_id=row.submitter_case_id,
        procedure_code=row.procedure_code,
        procedure_name=procedure.name if procedure else row.procedure_code,
        created_at=row.created_at,
        job_status=job.status,
        outcome=job.outcome,
        reviewer_outcome=job.review_outcome,
        extracted_by=job.system_name,
        error=job.error,
        pages=[PageView(number=p.number, text=p.text) for p in case.pages],
        criteria=criteria,
        reasons=_reasons(decision),
        injection_detected=bool(run and run.verified.injection_detected),
        injection_evidence=[_evidence(case, ev, (None, ev) not in invalid) for ev in injection],
        reviews=[
            ReviewView(
                id=r.id,
                action=r.action,  # type: ignore[arg-type]  # stored actions are Action values
                criterion_id=r.criterion_id,
                corrected_status=Status(r.corrected_status) if r.corrected_status else None,
                comment=r.comment,
                reviewer_id=r.reviewer_id,
                created_at=r.created_at,
            )
            for r in reviews
        ],
    )


@router.post(
    "/cases/{case_id}/reviews",
    status_code=201,
    response_model=ReviewView,
    responses={404: {"description": "unknown case"}, 409: {"description": "the case is not pended"}},
    summary="Record a review: approve, request more information, or flag an agent error",
)
async def add_review(
    request: Request, case_id: str, body: ReviewIn, principal: Annotated[Principal, REVIEWER]
) -> ReviewView:
    row, job, _ = await _load(request, case_id)
    if job.status not in ("done", "failed") or job.outcome != Outcome.PEND.value:
        raise HTTPException(status_code=409, detail="only a finished, pended case can be reviewed")
    if job.review_outcome:
        raise HTTPException(status_code=409, detail="a reviewer already approved this case")
    procedure = PROCEDURES.get(row.procedure_code)
    if body.criterion_id and (procedure is None or body.criterion_id not in procedure.criterion_ids):
        raise HTTPException(status_code=422, detail=f"{body.criterion_id!r} is not a criterion of this procedure")
    review = ReviewRow(
        id=str(uuid.uuid4()),
        case_id=case_id,
        reviewer_id=principal.user_id,
        action=body.action,
        criterion_id=body.criterion_id,
        corrected_status=body.corrected_status.value if body.corrected_status else None,
        comment=(body.reason if body.action == "request_info" else body.comment) or None,
        created_at=utcnow(),
    )
    if not await request.app.state.queue.record_review(review):  # e.g. another reviewer approved it meanwhile
        raise HTTPException(status_code=409, detail="the case changed; reload it")
    detail: dict[str, str] = {}
    if review.criterion_id:
        detail = {"criterion_id": review.criterion_id, "corrected_status": review.corrected_status or ""}
    if body.action == "approve":
        detail = {"outcome": "APPROVE", "job_id": job.id}  # the final decision changes: PEND (engine) -> APPROVE
    await _audit(request).record(principal.user_id, principal.role, f"review.{body.action}", case_id, **detail)
    sink: FeedbackSink = request.app.state.feedback
    await sink.write(
        {
            "review_id": review.id,
            "case_id": case_id,
            "submitter_case_id": row.submitter_case_id,
            "procedure_code": row.procedure_code,
            "action": review.action,
            "criterion_id": review.criterion_id,
            "corrected_status": review.corrected_status,
            "expected_decision": "APPROVE" if body.action == "approve" else None,
            "reviewed_at": review.created_at.isoformat(),
        }
    )
    return ReviewView(
        id=review.id,
        action=body.action,
        criterion_id=review.criterion_id,
        corrected_status=body.corrected_status,
        comment=review.comment,
        reviewer_id=review.reviewer_id,
        created_at=review.created_at,
    )
