"""Job queue in the database: idempotent submission, claiming with a lease, crash recovery.

Claiming. A worker selects the oldest claimable job, i.e. queued, or running with an expired lease (its worker died),
with `SELECT ... FOR UPDATE SKIP LOCKED` on Postgres so that concurrent workers skip each other's rows instead of
waiting. SQLite has no row locks (SQLAlchemy emits no FOR UPDATE there), so the claim is completed by an update
guarded by the row's version: of two workers that read the same row, only one update matches.

Leases. A running job holds a lease that its worker renews (heartbeat). If the worker crashes, the lease expires and
another worker reclaims the job; after `max_attempts` claims the job fails, and its decision is the fail-closed PEND.
Writing a result requires still holding the lease, so a worker that was presumed dead cannot overwrite the work of
the worker that took over.

Audit. With an AuditLog, every decision (job.decide) and every fail-closed failure (job.failed) is appended to the
hash-chained audit log once its transaction has committed.

Review. A reviewer's approval (record_review) is stored beside the engine's decision, never over it, and queues a
`case.reviewed` webhook in the same transaction as the review itself.
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Executable, Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from priorauth.api.fhir import ParsedBundle
from priorauth.jobs.db import CaseRow, JobRow, ReviewRow, WebhookDeliveryRow, utcnow
from priorauth.pipeline import CaseRun
from priorauth.security.audit import AuditLog

EVENT_DECIDED = "case.decided"
EVENT_REVIEWED = "case.reviewed"


class _ReviewConflict(Exception):
    pass


class IdempotencyConflict(Exception):
    """The idempotency key was already used with a different body."""


@dataclass(frozen=True)
class Submitted:
    case_id: str
    job_id: str
    created: bool  # False when the key was already known and the same body is replayed


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    case_id: str
    attempts: int
    case_json: str


async def _count(s: AsyncSession, stmt: Executable) -> int:
    """Rows matched by an UPDATE (the guard of every state change)."""
    return cast(CursorResult[Any], await s.execute(stmt)).rowcount


def body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def claimable(now: datetime) -> Select[str, str, str, int, int]:
    """The oldest queued job or running job whose lease expired, locked with SKIP LOCKED where supported."""
    return (
        select(JobRow.id, JobRow.case_id, JobRow.status, JobRow.attempts, JobRow.version)
        .where(
            or_(
                and_(JobRow.status == "queued", JobRow.available_at <= now),
                and_(JobRow.status == "running", JobRow.lease_expires_at < now),
            )
        )
        .order_by(JobRow.created_at, JobRow.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


class JobQueue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: float = 60.0,
        max_attempts: int = 3,
        webhook_url: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        audit: AuditLog | None = None,
    ) -> None:
        self.sessions = sessions
        self.audit = audit
        self.lease = timedelta(seconds=lease_seconds)
        self.max_attempts = max_attempts
        self.webhook_url = webhook_url
        self.clock = clock

    # -- submission ---------------------------------------------------------------------------------------------

    async def _audit(self, actor: str, action: str, case_id: str, **detail: Any) -> None:
        if self.audit is not None:
            await self.audit.record(actor, "system", action, case_id, **detail)

    async def submit(
        self, parsed: ParsedBundle, idempotency_key: str, digest: str, submitted_by: str | None = None
    ) -> Submitted:
        existing = await self._by_key(idempotency_key, digest)
        if existing is not None:
            return existing
        case, now = parsed.case, self.clock()
        job_id = str(uuid.uuid4())
        try:
            async with self.sessions.begin() as s:
                s.add(
                    CaseRow(
                        id=case.case_id,
                        idempotency_key=idempotency_key,
                        body_sha256=digest,
                        submitter_case_id=parsed.submitter_case_id,
                        submitted_by=submitted_by,
                        procedure_code=case.procedure_code,
                        member_id=case.patient.member_id,
                        case_json=case.model_dump_json(),
                        conditions_json=json.dumps(parsed.conditions),
                        created_at=now,
                    )
                )
                await s.flush()
                s.add(
                    JobRow(
                        id=job_id,
                        case_id=case.case_id,
                        status="queued",
                        created_at=now,
                        updated_at=now,
                        available_at=now,
                    )
                )
        except IntegrityError:  # a concurrent request with the same key won the insert
            existing = await self._by_key(idempotency_key, digest)
            if existing is None:
                raise
            return existing
        return Submitted(case_id=case.case_id, job_id=job_id, created=True)

    async def _by_key(self, key: str, digest: str) -> Submitted | None:
        async with self.sessions() as s:
            row = (
                await s.execute(
                    select(CaseRow.id, CaseRow.body_sha256, JobRow.id)
                    .join(JobRow, JobRow.case_id == CaseRow.id)
                    .where(CaseRow.idempotency_key == key)
                )
            ).first()
        if row is None:
            return None
        case_id, known_digest, job_id = row
        if known_digest != digest:
            raise IdempotencyConflict(key)
        return Submitted(case_id=case_id, job_id=job_id, created=False)

    # -- claiming -----------------------------------------------------------------------------------------------

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        """Claim the oldest claimable job, or return None. Jobs out of attempts are failed on the way."""
        while True:
            now = self.clock()
            failed, claimed = False, 0
            async with self.sessions.begin() as s:
                row = (await s.execute(claimable(now))).first()
                if row is None:
                    return None
                job_id, case_id, status, attempts, version = row
                guard = and_(JobRow.id == job_id, JobRow.version == version)
                failed = status == "running" and attempts >= self.max_attempts
                if failed:
                    await s.execute(
                        update(JobRow)
                        .where(guard)
                        .values(
                            status="failed",
                            error="max_attempts: worker lost",
                            outcome="PEND",
                            locked_by=None,
                            lease_expires_at=None,
                            version=version + 1,
                            updated_at=now,
                            finished_at=now,
                        )
                    )
                    self._enqueue_webhook(s, job_id, case_id, "PEND", now)
                else:
                    claimed = await _count(
                        s,
                        update(JobRow)
                        .where(guard)
                        .values(
                            status="running",
                            locked_by=worker_id,
                            lease_expires_at=now + self.lease,
                            attempts=attempts + 1,
                            version=version + 1,
                            updated_at=now,
                        ),
                    )
                    if claimed == 1:
                        case_json = (
                            await s.execute(select(CaseRow.case_json).where(CaseRow.id == case_id))
                        ).scalar_one()
            if failed:
                await self._audit("queue", "job.failed", case_id, job_id=job_id, error_kind="max_attempts")
                continue
            if claimed != 1:  # SQLite only: another worker updated the row first; look again
                continue
            return ClaimedJob(job_id=job_id, case_id=case_id, attempts=attempts + 1, case_json=case_json)

    async def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Renew the lease; False means the lease was lost (the job was reclaimed)."""
        now = self.clock()
        async with self.sessions.begin() as s:
            renewed = await _count(
                s,
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.locked_by == worker_id, JobRow.status == "running")
                .values(lease_expires_at=now + self.lease, updated_at=now),
            )
        return renewed == 1

    # -- completion ---------------------------------------------------------------------------------------------

    async def complete(self, job_id: str, worker_id: str, run: CaseRun, system_name: str | None = None) -> bool:
        """Store the result and queue the webhook, atomically; only while holding the lease."""
        now = self.clock()
        async with self.sessions.begin() as s:
            stored = await _count(
                s,
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.locked_by == worker_id, JobRow.status == "running")
                .values(
                    status="done",
                    outcome=run.decision.outcome.value,
                    result_json=run.model_dump_json(),
                    cost_usd=run.usage.cost_usd,
                    llm_calls=run.usage.llm_calls,
                    system_name=system_name,
                    error=run.error.split(":", 1)[0][:200] if run.error else None,
                    locked_by=None,
                    lease_expires_at=None,
                    version=JobRow.version + 1,
                    updated_at=now,
                    finished_at=now,
                ),
            )
            if stored != 1:
                return False
            self._enqueue_webhook(s, job_id, run.case_id, run.decision.outcome.value, now)
        await self._audit(
            f"worker:{worker_id}", "job.decide", run.case_id, job_id=job_id, outcome=run.decision.outcome.value
        )
        return True

    async def release(self, job_id: str, worker_id: str, error_kind: str, delay_s: float = 0.0) -> None:
        """Give a job back after an infrastructure error; it is retried after `delay_s` until max_attempts, then
        failed (fail-closed PEND)."""
        now = self.clock()
        async with self.sessions.begin() as s:
            job = (
                await s.execute(select(JobRow).where(JobRow.id == job_id, JobRow.locked_by == worker_id))
            ).scalar_one_or_none()
            if job is None or job.status != "running":
                return
            final = job.attempts >= self.max_attempts
            job.status = "failed" if final else "queued"
            job.available_at = now + timedelta(seconds=delay_s)
            job.error = error_kind[:200]
            job.locked_by, job.lease_expires_at = None, None
            job.version += 1
            job.updated_at = now
            if final:
                job.outcome, job.finished_at = "PEND", now
                self._enqueue_webhook(s, job_id, job.case_id, "PEND", now)
        if final:
            await self._audit(
                f"worker:{worker_id}", "job.failed", job.case_id, job_id=job_id, error_kind=error_kind[:80]
            )

    # -- human review -------------------------------------------------------------------------------------------

    async def record_review(self, review: ReviewRow) -> bool:
        """Store a review of a finished, pended case. An approval also records the reviewer's decision on the job
        (beside the engine's, which is kept) and queues a `case.reviewed` webhook, in the same transaction.
        False if the case is not finished, not pended, or already approved by a reviewer."""
        try:
            async with self.sessions.begin() as s:
                job = (
                    await s.execute(
                        select(JobRow.id, JobRow.status, JobRow.outcome, JobRow.review_outcome).where(
                            JobRow.case_id == review.case_id
                        )
                    )
                ).first()
                if job is None or job.status not in ("done", "failed") or job.outcome != "PEND" or job.review_outcome:
                    raise _ReviewConflict
                s.add(review)
                if review.action == "approve":
                    moved = await _count(
                        s,
                        update(JobRow)
                        .where(JobRow.id == job.id, JobRow.review_outcome.is_(None))
                        .values(
                            review_outcome="APPROVE",
                            review_id=review.id,
                            reviewed_by=review.reviewer_id,
                            reviewed_at=review.created_at,
                            version=JobRow.version + 1,
                            updated_at=review.created_at,
                        ),
                    )
                    if moved != 1:  # another reviewer approved it in between
                        raise _ReviewConflict
                    self._enqueue_webhook(
                        s,
                        job.id,
                        review.case_id,
                        "APPROVE",
                        review.created_at,
                        event=EVENT_REVIEWED,
                        extra={"decided_by": "reviewer", "engine_outcome": "PEND"},
                    )
        except _ReviewConflict:
            return False
        return True

    def _enqueue_webhook(
        self,
        s: AsyncSession,
        job_id: str,
        case_id: str,
        outcome: str,
        now: datetime,
        *,
        event: str = EVENT_DECIDED,
        extra: dict[str, str] | None = None,
    ) -> None:
        if not self.webhook_url:
            return
        delivery_id = str(uuid.uuid4())
        payload = {
            "event": event,
            "delivery_id": delivery_id,
            "case_id": case_id,
            "job_id": job_id,
            "outcome": outcome,
            "result_url": f"/cases/{case_id}/result",
        } | (extra or {})
        s.add(
            WebhookDeliveryRow(
                id=delivery_id,
                job_id=job_id,
                event=event,
                url=self.webhook_url,
                payload_json=json.dumps(payload, sort_keys=True),
                next_attempt_at=now,
                created_at=now,
            )
        )


@dataclass(frozen=True)
class DayCost:
    day: str
    jobs: int
    llm_calls: int
    cost_usd: float


async def daily_costs(sessions: async_sessionmaker[AsyncSession], since: datetime) -> list[DayCost]:
    """LLM cost of finished jobs per UTC day since `since` (grouped in Python: same code on SQLite and Postgres)."""
    async with sessions() as s:
        rows = (
            await s.execute(
                select(JobRow.finished_at, JobRow.cost_usd, JobRow.llm_calls).where(
                    JobRow.finished_at.is_not(None), JobRow.finished_at >= since
                )
            )
        ).all()
    days: dict[str, list[float]] = {}
    for finished, cost, calls in rows:
        if finished is None:  # excluded by the query; keeps the type checker informed
            continue
        acc = days.setdefault(finished.date().isoformat(), [0, 0, 0.0])
        acc[0] += 1
        acc[1] += calls
        acc[2] += cost
    return [
        DayCost(day=d, jobs=int(a[0]), llm_calls=int(a[1]), cost_usd=round(a[2], 6)) for d, a in sorted(days.items())
    ]


async def spent_today(sessions: async_sessionmaker[AsyncSession], now: datetime) -> float:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(d.cost_usd for d in await daily_costs(sessions, today))
