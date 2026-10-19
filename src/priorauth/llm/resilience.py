"""Retries, timeouts and a circuit breaker around a Transport (the real API transport in live and record modes).

- Retries: 429, 408, 409, 5xx (529 "overloaded" included), connection errors and timeouts are retried with
  exponential backoff and full jitter, honouring a `retry-after` header when the API sends one. Other errors (400,
  401, 403, 404, ...) are not retried. The SDK's own retries are turned off so that the two do not multiply.
- Timeout: each attempt is bounded by `timeout_s` (asyncio.wait_for), on top of the HTTP client's own timeout.
- Circuit breaker: after `failure_threshold` consecutive failed calls it opens and calls fail fast (CircuitOpen)
  for `cooldown_s`; then one trial call is let through (half-open); success closes it, failure re-opens it.

Errors surface as `RetriesExhausted` or `CircuitOpen`, which the worker treats as transient: the job goes back to
the queue with a delay instead of being decided (see priorauth.jobs.worker.TRANSIENT_KINDS).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from priorauth.llm.client import Transport

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class CircuitOpen(RuntimeError):
    pass


class RetriesExhausted(RuntimeError):
    def __init__(self, attempts: int, last: BaseException) -> None:
        super().__init__(f"{attempts} attempts failed, last error {type(last).__name__}")
        self.attempts = attempts
        self.last_kind = type(last).__name__


def classify(exc: BaseException) -> tuple[bool, float | None]:
    """(retryable, retry-after seconds or None). Works on the SDK's exceptions without importing them eagerly."""
    if isinstance(exc, TimeoutError):
        return True, None
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        retry_after = None
        response = getattr(exc, "response", None)
        header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        return status in RETRYABLE_STATUS, retry_after
    import anthropic

    return isinstance(exc, anthropic.APIConnectionError), None  # includes APITimeoutError


class CircuitBreaker:
    def __init__(
        self, failure_threshold: int = 5, cooldown_s: float = 30.0, clock: Callable[[], float] = time.monotonic
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.clock() - self.opened_at >= self.cooldown_s else "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise CircuitOpen(f"circuit open after {self.failures} consecutive failures")

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold or self.state == "half-open":
            self.opened_at = self.clock()


class ResilientTransport:
    def __init__(
        self,
        inner: Transport,
        *,
        max_attempts: int = 5,
        base_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        timeout_s: float = 120.0,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.inner = inner
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.timeout_s = timeout_s
        self.breaker = breaker or CircuitBreaker()
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.delays: list[float] = []  # every backoff actually waited (observability and tests)

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        """Full jitter in [0, min(cap, base * 2^(attempt-1))], never shorter than the server's retry-after."""
        delay = self.rng.uniform(0, min(self.max_delay_s, self.base_delay_s * 2.0 ** (attempt - 1)))
        return max(delay, retry_after or 0.0)

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.breaker.before_call()
        last: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await asyncio.wait_for(self.inner.send(request), timeout=self.timeout_s)
            except Exception as e:
                retryable, retry_after = classify(e)
                if not retryable:
                    self.breaker.failure()
                    raise
                last = e
                if attempt < self.max_attempts:
                    delay = self.backoff(attempt, retry_after)
                    self.delays.append(delay)
                    await self.sleep(delay)
                continue
            self.breaker.success()
            return response
        self.breaker.failure()
        if last is None:  # unreachable: the loop ran and every iteration failed
            raise RuntimeError("no attempt was made")
        raise RetriesExhausted(self.max_attempts, last) from last
