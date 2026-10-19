"""Hash-chained audit log of accesses and decisions (the principles of 45 CFR 164.312(b) and (c)(1)).

Each entry holds: seq (1, 2, ...), UTC time, actor (a user id or a worker id, and a role), action, resource (an
opaque id or a route template), a small JSON detail (status codes, outcomes, criterion ids: never record content,
checked by `detail_json`), the previous entry's hash and its own hash. The hash is HMAC-SHA256, keyed with
PRIORAUTH_AUDIT_KEY, of the previous hash and the canonical fields. A one-row head table holds the last seq and hash.

`verify()` recomputes the chain: a modified entry breaks its hash, a deleted or inserted entry breaks the sequence or
the link, a truncated tail no longer matches the head. Rebuilding a consistent chain and head needs the key. Without
PRIORAUTH_AUDIT_KEY the hash is a plain SHA-256 and the report says keyed: false: anyone able to write to the
database could then rewrite the whole log undetected.

Writes are appended in their own transaction right after the action is committed (a crash in between loses that one
entry), serialized in-process by a lock, across processes by a row lock on the head (Postgres) and a guarded update
(SQLite). The log is append-only by code; the database does not enforce it (no WORM storage, no external anchor).
"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import CursorResult, Integer, String, Text, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from priorauth.jobs.db import Base, utcnow

GENESIS = "0" * 64
MAX_DETAIL_STRING = 80
# Every key an entry's detail may carry; values are scalars. Free text (comments, quotes) never goes here.
DETAIL_KEYS = frozenset(
    {
        "status",
        "reason",
        "outcome",
        "replayed",
        "job_id",
        "case_id",
        "attempts",
        "error_kind",
        "entries",
        "ok",
        "first_bad_seq",
        "criterion_id",
        "corrected_status",
        "role",
    }
)
VERIFY_PAGE = 1000


class AuditRow(Base):
    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    ts: Mapped[datetime]
    actor_id: Mapped[str] = mapped_column(String(100))
    actor_role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(50))
    resource: Mapped[str] = mapped_column(String(100))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    __tablename__ = "audit_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    seq: Mapped[int] = mapped_column(Integer)
    hash: Mapped[str] = mapped_column(String(64))


class ForbiddenDetail(ValueError):
    pass


def detail_json(detail: dict[str, Any]) -> str:
    for key, value in detail.items():
        if key not in DETAIL_KEYS:
            raise ForbiddenDetail(f"audit detail {key!r} is not allowlisted")
        if not isinstance(value, str | int | float | bool | None) or (
            isinstance(value, str) and len(value) > MAX_DETAIL_STRING
        ):
            raise ForbiddenDetail(f"audit detail {key!r} has a disallowed value type or length")
    return json.dumps(detail, sort_keys=True, separators=(",", ":"))


def chain_hash(key: bytes | None, prev_hash: str, row: AuditRow) -> str:
    fields = [row.seq, row.ts.isoformat(), row.actor_id, row.actor_role, row.action, row.resource, row.detail_json]
    message = prev_hash.encode() + b"\n" + json.dumps(fields, separators=(",", ":")).encode()
    if key:
        return hmac.new(key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


class AuditEntry(BaseModel):
    seq: int
    ts: datetime
    actor_id: str
    actor_role: str
    action: str
    resource: str
    detail: dict[str, Any]


class AuditReport(BaseModel):
    ok: bool
    entries: int
    keyed: bool
    first_bad_seq: int | None = None
    reason: str | None = None  # missing_entry, broken_link, hash_mismatch, head_mismatch


class _Conflict(Exception):
    pass


class AuditLog:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], key: bytes | None, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self.sessions = sessions
        self.key = key or None
        self.clock = clock
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        try:
            async with self.sessions.begin() as s:
                if await s.get(AuditHead, 1) is None:
                    s.add(AuditHead(id=1, seq=0, hash=GENESIS))
        except IntegrityError:  # another process created it first
            pass

    async def record(self, actor_id: str, actor_role: str, action: str, resource: str, **detail: Any) -> int:
        """Append one entry and return its seq."""
        body = detail_json(detail)
        async with self._lock:
            for _ in range(20):
                try:
                    return await self._append(actor_id, actor_role, action, resource, body)
                except (_Conflict, IntegrityError):  # another process appended first: read the new head
                    await asyncio.sleep(0.01)
        raise RuntimeError("audit log append kept conflicting")

    async def _append(self, actor_id: str, actor_role: str, action: str, resource: str, body: str) -> int:
        async with self.sessions.begin() as s:
            locked = select(AuditHead.seq, AuditHead.hash).where(AuditHead.id == 1).with_for_update()
            head = (await s.execute(locked)).first()
            if head is None:
                raise RuntimeError("audit log not initialised (AuditLog.init)")
            prev_seq, prev_hash = head
            row = AuditRow(
                seq=prev_seq + 1,
                ts=self.clock(),
                actor_id=actor_id[:100],
                actor_role=actor_role[:20],
                action=action,
                resource=resource[:100],
                detail_json=body,
                prev_hash=prev_hash,
            )
            row.hash = chain_hash(self.key, prev_hash, row)
            s.add(row)
            moved = cast(
                CursorResult[Any],
                await s.execute(
                    update(AuditHead)
                    .where(AuditHead.id == 1, AuditHead.seq == prev_seq)
                    .values(seq=row.seq, hash=row.hash)
                ),
            ).rowcount
            if moved != 1:
                raise _Conflict
        return row.seq

    async def entries(self, after_seq: int = 0, limit: int = 100) -> list[AuditEntry]:
        async with self.sessions() as s:
            rows = (
                await s.execute(select(AuditRow).where(AuditRow.seq > after_seq).order_by(AuditRow.seq).limit(limit))
            ).scalars()
            return [
                AuditEntry(
                    seq=r.seq,
                    ts=r.ts,
                    actor_id=r.actor_id,
                    actor_role=r.actor_role,
                    action=r.action,
                    resource=r.resource,
                    detail=json.loads(r.detail_json),
                )
                for r in rows
            ]

    async def verify(self) -> AuditReport:
        keyed = self.key is not None
        expected, prev = 1, GENESIS

        def bad(seq: int, reason: str) -> AuditReport:
            return AuditReport(ok=False, entries=expected - 1, keyed=keyed, first_bad_seq=seq, reason=reason)

        async with self.sessions() as s:
            head = (await s.execute(select(AuditHead.seq, AuditHead.hash).where(AuditHead.id == 1))).first()
            while True:
                window = select(AuditRow).where(AuditRow.seq >= expected).order_by(AuditRow.seq).limit(VERIFY_PAGE)
                page = (await s.execute(window)).scalars().all()
                for row in page:
                    if row.seq != expected:
                        return bad(expected, "missing_entry")
                    if row.prev_hash != prev:
                        return bad(row.seq, "broken_link")
                    if not hmac.compare_digest(chain_hash(self.key, prev, row), row.hash):
                        return bad(row.seq, "hash_mismatch")
                    prev, expected = row.hash, expected + 1
                if len(page) < VERIFY_PAGE:
                    break
        if head is None or head[0] != expected - 1 or head[1] != prev:
            return bad(expected, "head_mismatch")
        return AuditReport(ok=True, entries=expected - 1, keyed=keyed)
