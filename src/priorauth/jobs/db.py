"""Database schema and engine. SQLite (aiosqlite) in tests and local runs, Postgres (asyncpg) in compose and cluster.

Timestamps are naive UTC everywhere (SQLite has no time zones; one convention avoids mixing them). Columns that hold
record content or identifiers are `EncryptedText` (Fernet, priorauth.security.crypto): the database only ever sees
ciphertext for them.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from priorauth.security.crypto import EncryptedText

# The database drivers log every statement *with its parameters* (record content) at DEBUG. An explicit level on
# their loggers wins over a DEBUG root logger, so turning on debug logging elsewhere cannot leak records.
for _driver_logger in ("aiosqlite", "asyncpg", "sqlalchemy.engine", "sqlalchemy.pool"):
    logging.getLogger(_driver_logger).setLevel(logging.WARNING)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CaseRow(Base):
    """One submitted Bundle. `case_json` holds the Case (patient and pages) as received, encrypted."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    body_sha256: Mapped[str] = mapped_column(String(64))
    submitter_case_id: Mapped[str | None] = mapped_column(String(200))
    submitted_by: Mapped[str | None] = mapped_column(String(36))  # user id; a `system` user only sees its own cases
    procedure_code: Mapped[str] = mapped_column(String(50))
    member_id: Mapped[str] = mapped_column(EncryptedText)
    case_json: Mapped[str] = mapped_column(EncryptedText)
    conditions_json: Mapped[str] = mapped_column(EncryptedText, default="[]")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class JobRow(Base):
    """Processing of one case. queued -> running -> done | failed; a running job holds a lease that it renews."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)  # optimistic lock, bumped by every state change
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(String(200))  # an error kind, never record content
    available_at: Mapped[datetime] = mapped_column(default=utcnow)  # retry backoff: not claimable before this
    cost_usd: Mapped[float] = mapped_column(default=0.0)  # LLM cost of the attempt that produced the result
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    system_name: Mapped[str | None] = mapped_column(String(100))  # the system under test that produced the result
    outcome: Mapped[str | None] = mapped_column(String(10))
    result_json: Mapped[str | None] = mapped_column(EncryptedText)  # findings with their quotes
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class WebhookDeliveryRow(Base):
    """Outbox of webhook notifications, written in the same transaction as the job result."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("job_id", "event"),
        Index("ix_deliveries_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    event: Mapped[str] = mapped_column(String(50))
    url: Mapped[str] = mapped_column(String(500))
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_status_code: Mapped[int | None]
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    delivered_at: Mapped[datetime | None]


class ReviewRow(Base):
    """One human review of a case: approve, request more information, or flag an agent error on one criterion."""

    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_case_created", "case_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    reviewer_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(20))  # approve | request_info | flag_error
    criterion_id: Mapped[str | None] = mapped_column(String(100))
    corrected_status: Mapped[str | None] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(EncryptedText)  # free text: may describe the record
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


def make_engine(url: str) -> AsyncEngine:
    """hide_parameters: SQL parameters (record content) never appear in SQLAlchemy logs or exception messages."""
    if url.startswith("sqlite"):
        # Writers queue behind each other instead of failing at once with "database is locked".
        return create_async_engine(url, connect_args={"timeout": 30}, hide_parameters=True)
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
