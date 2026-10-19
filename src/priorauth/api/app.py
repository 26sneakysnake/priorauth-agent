"""HTTP API: submit a FHIR Bundle, follow its job, read the decision as a ClaimResponse.

    uvicorn priorauth.api.app:app        # settings from PRIORAUTH_* environment variables (see Settings)

POST /cases requires an Idempotency-Key header: the same key with the same body returns the same job (202), with a
different body it is a 409. Bundle problems are 422s that list every error. Responses and logs carry identifiers,
statuses and reason codes only, never record content.
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from priorauth.api.fhir import BundleError, claim_response, parse_bundle
from priorauth.jobs.db import CaseRow, JobRow, create_schema, make_engine, sessions
from priorauth.jobs.queue import IdempotencyConflict, JobQueue, body_digest
from priorauth.jobs.webhooks import WebhookSender
from priorauth.jobs.worker import WorkerPool
from priorauth.pipeline import CaseRun, System
from priorauth.policy.engine import Decision, decide
from priorauth.policy.models import CaseFindings

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
    outcome: str | None
    job: JobView


class Problem(BaseModel):
    detail: Any


def default_system() -> System:
    """Production configuration: the gated agent (agent@v2 on Haiku), LLM mode from LLM_MODE."""
    from priorauth.agent.loop import AgentSystem
    from priorauth.llm.pricing import HAIKU

    return AgentSystem("agent_v2_haiku", HAIKU, "agent@v2", case_cap_usd=0.10)


def decision_of(job: JobRow, case: CaseRow) -> Decision:
    if job.status == "done" and job.result_json:
        return CaseRun.model_validate_json(job.result_json).decision
    # Failed after every attempt: fail closed, as if the system had produced no findings.
    return decide(CaseFindings(procedure_code=case.procedure_code, findings=()))


def create_app(
    settings: Settings | None = None,
    system_factory: Callable[[], System] = default_system,
    webhook_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        await create_schema(engine)
        session_maker = sessions(engine)
        queue = JobQueue(
            session_maker,
            lease_seconds=settings.lease_seconds,
            max_attempts=settings.max_attempts,
            webhook_url=settings.webhook_url,
        )
        app.state.queue = queue
        app.state.sessions = session_maker
        stop = asyncio.Event()
        background: list[asyncio.Task[None]] = []
        client = webhook_client or httpx.AsyncClient()
        if settings.run_workers:
            app.state.pool = WorkerPool(queue, system_factory(), concurrency=settings.workers)
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
            submitted = await queue.submit(parsed, idempotency_key, digest)
        except IdempotencyConflict:
            return JSONResponse(
                status_code=409, content={"detail": "this Idempotency-Key was already used with a different body"}
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

    async def load(request: Request, case_id: str) -> tuple[CaseRow, JobRow]:
        async with request.app.state.sessions() as s:
            row = (
                await s.execute(
                    select(CaseRow, JobRow).join(JobRow, JobRow.case_id == CaseRow.id).where(CaseRow.id == case_id)
                )
            ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown case")
        return row[0], row[1]

    @app.get("/cases/{case_id}", response_model=CaseView, responses={404: {"model": Problem}})
    async def get_case(request: Request, case_id: str) -> CaseView:
        case, job = await load(request, case_id)
        return CaseView(
            case_id=case.id,
            submitter_case_id=case.submitter_case_id,
            procedure_code=case.procedure_code,
            created_at=case.created_at,
            outcome=job.outcome,
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
    async def get_result(request: Request, case_id: str) -> JSONResponse:
        case, job = await load(request, case_id)
        if job.status not in ("done", "failed"):
            return JSONResponse(status_code=409, content={"detail": f"job is {job.status}"})
        body = claim_response(case.id, case.member_id, decision_of(job, case), job.finished_at or job.updated_at)
        return JSONResponse(content=body, media_type="application/fhir+json")

    return app


def __getattr__(name: str) -> Any:  # `uvicorn priorauth.api.app:app` builds the app lazily from the environment
    if name == "app":
        return create_app()
    raise AttributeError(name)
