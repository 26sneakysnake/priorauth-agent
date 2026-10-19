"""Demo routes, mounted only by the demo server (python -m priorauth.demo).

GET  /demo/info                     public: demo account, live-mode status, reset notice
GET  /demo/examples                 public: the example cases and what each one tests
POST /demo/examples/{id}/run        reviewer: submit the example again; the worker decides it (replayed model)
POST /demo/live                     reviewer: run a visitor's synthetic text with the real model (off by default)
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from priorauth.api.deps import REVIEWER
from priorauth.api.fhir import case_to_bundle, parse_bundle
from priorauth.api.review import CriterionView, EvidenceView, PageView, ReasonView, criterion_views, injection_views
from priorauth.api.review import reason_views as reasons_of
from priorauth.data.schema import Case, Gold
from priorauth.demo.examples import Example
from priorauth.demo.live import LiveRefused, LiveRunner
from priorauth.jobs.db import CaseRow
from priorauth.jobs.queue import JobQueue, body_digest
from priorauth.policy.criteria import PROCEDURES
from priorauth.security.audit import AuditLog
from priorauth.security.auth import Principal

MAX_DEMO_CASES = 2000  # the database is a file in the container; past this, runs are refused until a restart
NOTICE = "Demonstration on synthetic data. Every restart resets the demo; nothing you do here is kept."

router = APIRouter(prefix="/demo", tags=["demo"])


@dataclass
class DemoState:
    examples: list[tuple[Example, Case, Gold]]
    system_user_id: str
    reviewer_username: str
    reviewer_password: str
    system_name: str
    live: LiveRunner
    seeded: dict[str, str] = field(default_factory=dict)  # example id -> case id decided at start


class DemoInfo(BaseModel):
    notice: str
    reviewer_username: str
    reviewer_password: str
    extracted_by: str
    live_enabled: bool
    live_max_chars: int
    resets_on_restart: bool = True


class ExampleView(BaseModel):
    id: str
    title: str
    label: str
    look_for: str
    procedure_code: str
    procedure_name: str
    expected_decision: str  # the gold label of the synthetic case
    seeded_case_id: str | None


class RunAccepted(BaseModel):
    example_id: str
    case_id: str


class LiveIn(BaseModel):
    procedure_code: str
    record_text: str = Field(description="Synthetic record text; pages separated by form feeds. Never real data.")


class LiveResult(BaseModel):
    outcome: str
    procedure_code: str
    reasons: list[ReasonView]
    criteria: list[CriterionView]
    pages: list[PageView]
    injection_detected: bool
    injection_evidence: list[EvidenceView]
    cost_usd: float
    llm_calls: int
    notice: str = "Not stored: this result exists only in this response."


def _state(request: Request) -> DemoState:
    state: DemoState = request.app.state.demo
    return state


def _audit(request: Request) -> AuditLog:
    audit: AuditLog = request.app.state.audit
    return audit


def client_ip(request: Request, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for and (forwarded := request.headers.get("x-forwarded-for")):
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


@router.get("/info", response_model=DemoInfo, summary="Demo account and settings (public)")
async def info(request: Request) -> DemoInfo:
    state = _state(request)
    return DemoInfo(
        notice=NOTICE,
        reviewer_username=state.reviewer_username,
        reviewer_password=state.reviewer_password,
        extracted_by=state.system_name,
        live_enabled=state.live.config.enabled,
        live_max_chars=state.live.config.max_chars,
    )


@router.get("/examples", response_model=list[ExampleView], summary="Example cases (public)")
async def examples(request: Request) -> list[ExampleView]:
    state = _state(request)
    return [
        ExampleView(
            id=ex.id,
            title=ex.title,
            label=ex.label,
            look_for=ex.look_for,
            procedure_code=case.procedure_code,
            procedure_name=PROCEDURES[case.procedure_code].name,
            expected_decision=gold.expected_decision,
            seeded_case_id=state.seeded.get(ex.id),
        )
        for ex, case, gold in state.examples
    ]


@router.post(
    "/examples/{example_id}/run",
    status_code=202,
    response_model=RunAccepted,
    responses={404: {"description": "unknown example"}, 503: {"description": "the demo database is full"}},
    summary="Run the agent on an example (a new submission, decided from recorded responses)",
)
async def run_example(request: Request, example_id: str, principal: Annotated[Principal, REVIEWER]) -> RunAccepted:
    state = _state(request)
    found = next(((ex, case) for ex, case, _ in state.examples if ex.id == example_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown example")
    example, case = found
    async with request.app.state.sessions() as s:
        count = (await s.execute(select(func.count()).select_from(CaseRow))).scalar_one()
    if count >= MAX_DEMO_CASES:
        raise HTTPException(status_code=503, detail="the demo is full; it resets on its next restart")
    bundle = case_to_bundle(case, case.case_id)
    parsed = parse_bundle(bundle, str(uuid.uuid4()))
    queue: JobQueue = request.app.state.queue
    key = f"try-{example.id}-{uuid.uuid4()}"
    digest = body_digest(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode() + key.encode())
    submitted = await queue.submit(parsed, key, digest, submitted_by=state.system_user_id)
    await _audit(request).record(principal.user_id, principal.role, "demo.run", submitted.case_id)
    return RunAccepted(example_id=example.id, case_id=submitted.case_id)


@router.post(
    "/live",
    response_model=LiveResult,
    responses={
        404: {"description": "live mode is disabled (the default)"},
        413: {"description": "text too long"},
        429: {"description": "rate limited"},
        502: {"description": "model run failed (no decision)"},
        503: {"description": "daily budget spent"},
    },
    summary="Run the real model on your own SYNTHETIC record (off unless DEMO_LIVE_ENABLED=true)",
)
async def live(request: Request, body: LiveIn, principal: Annotated[Principal, REVIEWER]) -> LiveResult:
    state = _state(request)
    audit = _audit(request)
    if body.procedure_code not in PROCEDURES:
        raise HTTPException(status_code=422, detail="unknown procedure")
    ip = client_ip(request, state.live.config.trust_forwarded_for)
    try:
        case, run = await state.live.run(body.procedure_code, body.record_text, ip)
    except LiveRefused as e:
        await audit.record(
            principal.user_id, principal.role, "demo.live", "/demo/live", status=e.status, reason=e.reason
        )
        raise HTTPException(status_code=e.status, detail=e.detail) from None
    await audit.record(
        principal.user_id, principal.role, "demo.live", "/demo/live", status=200, outcome=run.decision.outcome.value
    )
    return LiveResult(
        outcome=run.decision.outcome.value,
        procedure_code=case.procedure_code,
        reasons=reasons_of(run.decision),
        criteria=criterion_views(case, run.decision, run),
        pages=[PageView(number=p.number, text=p.text) for p in case.pages],
        injection_detected=run.verified.injection_detected,
        injection_evidence=injection_views(case, run),
        cost_usd=run.usage.cost_usd,
        llm_calls=run.usage.llm_calls,
    )
