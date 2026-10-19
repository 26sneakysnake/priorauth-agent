"""Outbound webhooks: HMAC-signed, retried with exponential backoff and jitter, delivered from an outbox table.

Signature header (Stripe-style): `X-PriorAuth-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "t.body")>`.
The timestamp is signed with the body, so a receiver can reject a replayed request (`verify` with a tolerance);
`X-PriorAuth-Delivery` lets it drop duplicates, since a delivery can be retried after a timeout that did reach it.
Payloads carry identifiers and the outcome only, never record content.
"""

import asyncio
import hashlib
import hmac
import random
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from priorauth.jobs.db import WebhookDeliveryRow, utcnow

SIGNATURE_HEADER = "X-PriorAuth-Signature"
DELIVERY_HEADER = "X-PriorAuth-Delivery"
EVENT_HEADER = "X-PriorAuth-Event"


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: bytes, header: str, body: bytes, *, now: float | None = None, tolerance_s: int = 300) -> bool:
    """Receiver side: True if the signature matches and the timestamp is recent."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - timestamp) > tolerance_s:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)


def backoff_seconds(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Delay after the `attempt`-th failure: exponential, capped, with equal jitter (half fixed, half random)."""
    ceiling = min(cap, base * 2.0 ** (attempt - 1))
    return ceiling / 2 + rng.uniform(0, ceiling / 2)


class WebhookSender:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret: bytes,
        client: httpx.AsyncClient,
        *,
        base_delay_s: float = 2.0,
        max_delay_s: float = 600.0,
        max_attempts: int = 8,
        timeout_s: float = 10.0,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.sessions = sessions
        self.secret = secret
        self.client = client
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.clock = clock
        self.rng = rng or random.Random()

    async def deliver_due(self, limit: int = 20) -> int:
        """Attempt every delivery that is due; returns how many were attempted."""
        now = self.clock()
        async with self.sessions.begin() as s:
            rows = (
                (
                    await s.execute(
                        select(WebhookDeliveryRow)
                        .where(WebhookDeliveryRow.status == "pending", WebhookDeliveryRow.next_attempt_at <= now)
                        .order_by(WebhookDeliveryRow.next_attempt_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            # Short lease: another sender skips these rows while they are in flight.
            for row in rows:
                row.next_attempt_at = now + timedelta(seconds=self.timeout_s + 5)
        for row in rows:
            await self._attempt(row)
        return len(rows)

    async def _attempt(self, row: WebhookDeliveryRow) -> None:
        body = row.payload_json.encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self.secret, int(time.time()), body),
            DELIVERY_HEADER: row.id,
            EVENT_HEADER: row.event,
        }
        status_code: int | None = None
        error: str | None = None
        try:
            response = await self.client.post(row.url, content=body, headers=headers, timeout=self.timeout_s)
            status_code = response.status_code
            ok = 200 <= status_code < 300
            if not ok:
                error = f"http {status_code}"
        except httpx.HTTPError as e:
            ok, error = False, type(e).__name__
        now = self.clock()
        async with self.sessions.begin() as s:
            current = await s.get(WebhookDeliveryRow, row.id)
            if current is None or current.status != "pending":
                return
            current.attempts += 1
            current.last_status_code, current.last_error = status_code, error
            if ok:
                current.status, current.delivered_at = "delivered", now
            elif current.attempts >= self.max_attempts:
                current.status = "failed"
            else:
                delay = backoff_seconds(current.attempts, base=self.base_delay_s, cap=self.max_delay_s, rng=self.rng)
                current.next_attempt_at = now + timedelta(seconds=delay)

    async def run(self, stop: asyncio.Event, poll_s: float = 0.5) -> None:
        while not stop.is_set():
            await self.deliver_due()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_s)
