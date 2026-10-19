"""HTTP API: submit a FHIR Bundle, follow its job, read the decision as a ClaimResponse.

    uvicorn priorauth.api.app:app        # settings from PRIORAUTH_* environment variables (see Settings)

POST /cases requires an Idempotency-Key header: the same key with the same body returns the same job (202), with a
different body it is a 409. Bundle problems are 422s that list every error. Responses and logs carry identifiers,
statuses and reason codes only, never record content.

Every route but /health and /auth/token needs a bearer JWT from POST /auth/token, and a role (priorauth.security.auth):

    POST /cases                      system, admin         (a system user only sees the cases it submitted)
    GET  /cases/{id}, .../result     system, reviewer, admin
    GET  /review/queue, /cases/{id}/review, POST /cases/{id}/reviews   reviewer, admin (priorauth.api.review)
    GET  /costs/daily, /audit, /audit/verify               admin

Accesses, refusals and decisions are written to the hash-chained audit log (priorauth.security.audit). Record
fields are encrypted at rest (priorauth.security.crypto): the app refuses to start without PRIORAUTH_FIELD_KEY.
"""

import asyncio
import json
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from priorauth.api import review
from priorauth.api.deps import ADMIN, READER, SUBMITTER, UUID_SHAPE
from priorauth.api.fhir import BundleError, ReviewerDecision, claim_response, parse_bundle
from priorauth.api.review import DEFAULT_FEEDBACK_PATH, FeedbackSink
from priorauth.jobs.db import CaseRow, JobRow, create_schema, make_engine, sessions, utcnow
from priorauth.jobs.queue import IdempotencyConflict, JobQueue, body_digest, daily_costs
from priorauth.jobs.webhooks import WebhookSender
from priorauth.jobs.worker import DailyBudget, WorkerPool
from priorauth.pipeline import CaseRun, System
from priorauth.policy.engine import Decision, decide
from priorauth.policy.models import CaseFindings
from priorauth.security import crypto
from priorauth.security.audit import AuditEntry, AuditLog, AuditReport
from priorauth.security.auth import DEMO_USERS, TOKEN_TTL, AuthError, Principal, Role, TokenIssuer, Users

log = logging.getLogger("priorauth.api")


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite+aiosqlite:///./priorauth.db"
    workers: int = 4
    run_workers: bool = True
    lease_seconds: float = 60.0
    max_attempts: int = 3
    webhook_url: str | None = None
    webhook_secret: str = ""
    daily_cost_cap_usd: float | None = None
    case_cost_cap_usd: float = 0.10
    jwt_secret: str = ""  # empty: a random per-process secret (tokens die with the process)
    audit_key: str = ""  # empty: unkeyed audit hashes, reported as keyed: false
    demo_password: str | None = None  # set: seed demo-system, demo-reviewer and demo-admin with this password
    review_feedback_path: str | None = DEFAULT_FEEDBACK_PATH  # one JSON line per review; None: not written
    cors_origins: tuple[str, ...] = ()  # browser origins allowed to call the API directly (the UI proxies instead)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            database_url=env("PRIORAUTH_DATABASE_URL", cls.database_url),
            workers=int(env("PRIORAUTH_WORKERS", str(cls.workers))),
            run_workers=env("PRIORAUTH_RUN_WORKERS", "1") == "1",
            lease_seconds=float(env("PRIORAUTH_LEASE_SECONDS", str(cls.lease_seconds))),
            max_attempts=int(env("PRIORAUTH_MAX_ATTEMPTS", str(cls.max_attempts))),
            webhook_url=env("PRIORAUTH_WEBHOOK_URL") or None,
            webhook_secret=env("PRIORAUTH_WEBHOOK_SECRET", ""),
            daily_cost_cap_usd=float(cap) if (cap := env("PRIORAUTH_DAILY_COST_CAP_USD")) else None,
            case_cost_cap_usd=float(env("PRIORAUTH_CASE_COST_CAP_USD", str(cls.case_cost_cap_usd))),
            jwt_secret=env("PRIORAUTH_JWT_SECRET", ""),
            audit_key=env("PRIORAUTH_AUDIT_KEY", ""),
            demo_password=env("PRIORAUTH_DEMO_PASSWORD") or None,
            review_feedback_path=env("PRIORAUTH_REVIEW_FEEDBACK_PATH", DEFAULT_FEEDBACK_PATH) or None,
            cors_origins=tuple(o.strip() for o in env("PRIORAUTH_CORS_ORIGINS", "").split(",") if o.strip()),
        )


class CaseAccepted(BaseModel):
    case_id: str
    job_id: str
    status: str
    status_url: str
    result_url: str


class JobView(BaseModel):
    id: str
    status: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    error: str | None


class CaseView(BaseModel):
    case_id: str
    submitter_case_id: str | None
    procedure_code: str
    created_at: datetime
    outcome: str | None  # the final decision: the reviewer's if a reviewer approved, else the engine's
    engine_outcome: str | None  # what the policy engine decided (kept after a review)
    reviewer_outcome: str | None  # APPROVE once a reviewer approved the pended case
    decided_by: str | None  # "engine" or "reviewer"
    job: JobView


class Problem(BaseModel):
    detail: Any


class DailyCost(BaseModel):
    day: str
    jobs: int
    llm_calls: int
    cost_usd: float


class CostReport(BaseModel):
    daily_cap_usd: float | None
    case_cap_usd: float
    days: list[DailyCost]


class Credentials(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  # the OAuth 2 token type, not a secret
    expires_in: int
    role: Role


class Me(BaseModel):
    user_id: str
    username: str
    role: Role


def default_system(case_cap_usd: float = 0.10) -> System:
    """Production configuration: the gated agent (agent@v2 on Haiku), LLM mode from LLM_MODE, behind the model view
    (minimum necessary pages, redacted identifiers; priorauth.security.model_view)."""
    from priorauth.agent.loop import AgentSystem
    from priorauth.llm.pricing import HAIKU
    from priorauth.security.model_view import ProtectedSystem

    return ProtectedSystem(AgentSystem("agent_v2_haiku", HAIKU, "agent@v2", case_cap_usd=case_cap_usd))


def decision_of(job: JobRow, case: CaseRow) -> Decision:
    if job.status == "done" and job.result_json:
        return CaseRun.model_validate_json(job.result_json).decision
    # Failed after every attempt: fail closed, as if the system had produced no findings.
    return decide(CaseFindings(procedure_code=case.procedure_code, findings=()))


def create_app(
    settings: Settings | None = None,
    system_factory: Callable[[], System] | None = None,
    webhook_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    cfg = settings

    def make_system() -> System:
        return system_factory() if system_factory is not None else default_system(cfg.case_cost_cap_usd)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        crypto.cipher()  # fail fast: without PRIORAUTH_FIELD_KEY no record could be stored
        engine = make_engine(settings.database_url)
        await create_schema(engine)
        session_maker = sessions(engine)
        audit = AuditLog(session_maker, settings.audit_key.encode() or None)
        await audit.init()
        if not settings.audit_key:
            log.warning("PRIORAUTH_AUDIT_KEY is not set: audit hashes are unkeyed")
        users = Users(session_maker)
        if not settings.jwt_secret:
            log.warning("PRIORAUTH_JWT_SECRET is not set: tokens are signed with a random per-process secret")
        app.state.tokens = TokenIssuer(settings.jwt_secret.encode() or secrets.token_bytes(32))
        app.state.users, app.state.audit = users, audit
        app.state.feedback = FeedbackSink(settings.review_feedback_path)
        if settings.demo_password:
            for username, role in DEMO_USERS:
                await users.ensure(username, settings.demo_password, role)
        queue = JobQueue(
            session_maker,
            lease_seconds=settings.lease_seconds,
            max_attempts=settings.max_attempts,
            webhook_url=settings.webhook_url,
            audit=audit,
        )
        app.state.queue = queue
        app.state.sessions = session_maker
        stop = asyncio.Event()
        background: list[asyncio.Task[None]] = []
        client = webhook_client or httpx.AsyncClient()
        if settings.run_workers:
            budget = (
                DailyBudget(settings.daily_cost_cap_usd, settings.case_cost_cap_usd)
                if settings.daily_cost_cap_usd is not None
                else None
            )
            app.state.pool = WorkerPool(queue, make_system(), concurrency=settings.workers, budget=budget)
            background.append(asyncio.create_task(app.state.pool.run(stop)))
            if settings.webhook_url:
                sender = WebhookSender(session_maker, settings.webhook_secret.encode(), client)
                background.append(asyncio.create_task(sender.run(stop)))
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(*background, return_exceptions=True)
            if webhook_client is None:
                await client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="priorauth-agent",
        version="0.4.0",
        summary="Prior-authorization agent on synthetic records: the LLM extracts cited evidence, a deterministic "
        "engine decides APPROVE or PEND (never deny).",
        lifespan=lifespan,
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz", summary="Liveness: the process serves requests (the demo opens its port once seeded)")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/auth/token",
        response_model=TokenResponse,
        responses={401: {"model": Problem}},
        summary="Exchange a username and password for a bearer JWT (15 minutes)",
    )
    async def token(request: Request, credentials: Credentials) -> TokenResponse:
        users: Users = request.app.state.users
        audit: AuditLog = request.app.state.audit
        try:
            user = await users.authenticate(credentials.username, credentials.password)
        except AuthError as e:
            await audit.record("anonymous", "none", "auth.failed", "/auth/token", reason=e.reason)
            raise HTTPException(
                status_code=401, detail="invalid credentials", headers={"WWW-Authenticate": "Bearer"}
            ) from None
        await audit.record(user.id, user.role, "auth.login", "/auth/token")
        return TokenResponse(
            access_token=request.app.state.tokens.issue(user),
            expires_in=int(TOKEN_TTL.total_seconds()),
            role=user.role,  # type: ignore[arg-type]  # a stored role is one of ROLES
        )

    @app.get("/auth/me", response_model=Me, summary="Who the bearer token belongs to")
    async def me(principal: Annotated[Principal, READER]) -> Me:
        return Me(user_id=principal.user_id, username=principal.username, role=principal.role)

    @app.get("/costs/daily", response_model=CostReport, summary="LLM cost of finished jobs per UTC day")
    async def costs(request: Request, principal: Annotated[Principal, ADMIN], days: int = 7) -> CostReport:
        since = utcnow() - timedelta(days=max(1, min(days, 90)))
        rows = await daily_costs(request.app.state.sessions, since)
        await request.app.state.audit.record(principal.user_id, principal.role, "costs.read", "/costs/daily")
        return CostReport(
            daily_cap_usd=settings.daily_cost_cap_usd,
            case_cap_usd=settings.case_cost_cap_usd,
            days=[DailyCost(day=r.day, jobs=r.jobs, llm_calls=r.llm_calls, cost_usd=r.cost_usd) for r in rows],
        )

    @app.post(
        "/cases",
        status_code=202,
        response_model=CaseAccepted,
        responses={400: {"model": Problem}, 409: {"model": Problem}, 422: {"model": Problem}},
        summary="Submit a FHIR R4B Bundle for prior-authorization review",
    )
    async def submit_case(
        request: Request,
        response: Response,
        principal: Annotated[Principal, SUBMITTER],
        bundle: Annotated[dict[str, Any], Body(description="FHIR Bundle (type collection), see priorauth.api.fhir")],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CaseAccepted | JSONResponse:
        if not idempotency_key or len(idempotency_key) > 200:
            return JSONResponse(status_code=400, content={"detail": "Idempotency-Key header (1-200 chars) is required"})
        digest = body_digest(json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode())
        case_id = str(uuid.uuid4())
        try:
            parsed = parse_bundle(bundle, case_id)
        except BundleError as e:
            return JSONResponse(status_code=422, content={"detail": e.errors})
        queue: JobQueue = request.app.state.queue
        try:
            submitted = await queue.submit(parsed, idempotency_key, digest, submitted_by=principal.user_id)
        except IdempotencyConflict:
            return JSONResponse(
                status_code=409, content={"detail": "this Idempotency-Key was already used with a different body"}
            )
        await request.app.state.audit.record(
            principal.user_id, principal.role, "case.submit", submitted.case_id, replayed=not submitted.created
        )
        log.info(
            "case submitted",
            extra={"case_id": submitted.case_id, "job_id": submitted.job_id, "replayed": not submitted.created},
        )
        response.headers["Location"] = f"/cases/{submitted.case_id}"
        return CaseAccepted(
            case_id=submitted.case_id,
            job_id=submitted.job_id,
            status="queued" if submitted.created else "accepted (replayed key)",
            status_url=f"/cases/{submitted.case_id}",
            result_url=f"/cases/{submitted.case_id}/result",
        )

    async def load(request: Request, case_id: str, principal: Principal, action: str) -> tuple[CaseRow, JobRow]:
        """The case and its job, audited; a `system` user gets a 404 for a case it did not submit."""
        async with request.app.state.sessions() as s:
            row = (
                await s.execute(
                    select(CaseRow, JobRow).join(JobRow, JobRow.case_id == CaseRow.id).where(CaseRow.id == case_id)
                )
            ).first()
        audit: AuditLog = request.app.state.audit
        if row is None or (principal.role == "system" and row[0].submitted_by != principal.user_id):
            probed = case_id if UUID_SHAPE.fullmatch(case_id) else "malformed-id"  # path text is caller input
            await audit.record(principal.user_id, principal.role, action, probed, status=404)
            raise HTTPException(status_code=404, detail="unknown case")
        await audit.record(principal.user_id, principal.role, action, case_id, status=200)
        return row[0], row[1]

    @app.get("/cases/{case_id}", response_model=CaseView, responses={404: {"model": Problem}})
    async def get_case(request: Request, case_id: str, principal: Annotated[Principal, READER]) -> CaseView:
        case, job = await load(request, case_id, principal, "case.read")
        return CaseView(
            case_id=case.id,
            submitter_case_id=case.submitter_case_id,
            procedure_code=case.procedure_code,
            created_at=case.created_at,
            outcome=job.review_outcome or job.outcome,
            engine_outcome=job.outcome,
            reviewer_outcome=job.review_outcome,
            decided_by=("reviewer" if job.review_outcome else "engine") if job.outcome else None,
            job=JobView(
                id=job.id,
                status=job.status,
                attempts=job.attempts,
                created_at=job.created_at,
                updated_at=job.updated_at,
                finished_at=job.finished_at,
                error=job.error,
            ),
        )

    @app.get(
        "/cases/{case_id}/result",
        responses={
            200: {"description": "Simplified ClaimResponse (FHIR R4B), inspired by Da Vinci PAS, not certified"},
            404: {"model": Problem},
            409: {"model": Problem, "description": "the job is not finished"},
        },
    )
    async def get_result(request: Request, case_id: str, principal: Annotated[Principal, READER]) -> JSONResponse:
        case, job = await load(request, case_id, principal, "result.read")
        if job.status not in ("done", "failed"):
            return JSONResponse(status_code=409, content={"detail": f"job is {job.status}"})
        reviewer = (
            ReviewerDecision(review_id=job.review_id, reviewer_id=job.reviewed_by, decided_at=job.reviewed_at)
            if job.review_outcome == "APPROVE" and job.review_id and job.reviewed_by and job.reviewed_at
            else None
        )
        body = claim_response(
            case.id, case.member_id, decision_of(job, case), job.finished_at or job.updated_at, reviewer
        )
        return JSONResponse(content=body, media_type="application/fhir+json")

    @app.get("/audit", response_model=list[AuditEntry], summary="Audit log entries after a sequence number")
    async def audit_entries(
        request: Request, principal: Annotated[Principal, ADMIN], after_seq: int = 0, limit: int = 100
    ) -> list[AuditEntry]:
        audit: AuditLog = request.app.state.audit
        entries = await audit.entries(after_seq=max(0, after_seq), limit=max(1, min(limit, 1000)))
        await audit.record(principal.user_id, principal.role, "audit.read", "/audit", entries=len(entries))
        return entries

    @app.get("/audit/verify", response_model=AuditReport, summary="Recompute the audit hash chain")
    async def audit_verify(request: Request, principal: Annotated[Principal, ADMIN]) -> AuditReport:
        audit: AuditLog = request.app.state.audit
        report = await audit.verify()
        await audit.record(
            principal.user_id, principal.role, "audit.verify", "/audit/verify", ok=report.ok, entries=report.entries
        )
        return report

    app.include_router(review.router)
    return app


def __getattr__(name: str) -> Any:  # `uvicorn priorauth.api.app:app` builds the app lazily from the environment
    if name == "app":
        from priorauth.obs import logs, tracing

        logs.configure()
        tracing.configure()
        return create_app()
    raise AttributeError(name)
