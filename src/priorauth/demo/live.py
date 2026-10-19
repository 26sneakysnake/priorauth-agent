"""The demo's "your own record" mode: a visitor's synthetic text, run once through the real model. OFF by default.

    DEMO_LIVE_ENABLED=true                      # anything else, or unset: off (the route answers 404)
    DEMO_LIVE_DAILY_CAP_USD=1.0                 # model spend per UTC day, all visitors together
    DEMO_LIVE_CASE_CAP_USD=0.05                 # per record
    DEMO_LIVE_MAX_CHARS=12000                   # text size
    DEMO_LIVE_PER_IP_PER_HOUR=5                 # runs per client IP per rolling hour
    DEMO_LIVE_TRUST_FORWARDED_FOR=false         # true behind a proxy that sets X-Forwarded-For (Container Apps, Vercel)

Fail-closed: a run that hits a cap, a rate limit, a timeout or a model error returns an error and no decision; the
visitor goes back to the examples. Nothing typed is kept: the text is not written to the database, the logs, the
audit log or a cassette; it lives in the request and the response only. The spend counter and the rate limiter are in
memory: a restart resets them (with one replica, a restart is not something a visitor can trigger).

The text has no structured patient header, so the redactor can only remove phone numbers from it: the UI and the API
say "synthetic data only, never type real data".
"""

import asyncio
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from priorauth.agent.loop import AgentSystem
from priorauth.data.schema import Case, Page, Patient
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, SpendLedger, Transport
from priorauth.llm.fake import NO_CASSETTES
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import CaseRun, run_case
from priorauth.security.model_view import ProtectedSystem

MAX_PAGES = 20
PAGE_BREAK = "\f"
TIMEOUT_S = 180.0
# A placeholder patient: the text has no header to take identifiers from, and these values cannot occur in it.
LIVE_PATIENT = Patient(
    name="Xq Zv", birth_date=date(1900, 1, 1), sex="female", mrn="NO-MRN-LIVE", member_id="NO-MEMBER-LIVE"
)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class LiveConfig:
    enabled: bool = False
    daily_cap_usd: float = 1.0
    case_cap_usd: float = 0.05
    max_chars: int = 12_000
    per_ip_per_hour: int = 5
    trust_forwarded_for: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "LiveConfig":
        return cls(
            enabled=_flag(environ.get("DEMO_LIVE_ENABLED")),
            daily_cap_usd=float(environ.get("DEMO_LIVE_DAILY_CAP_USD", cls.daily_cap_usd)),
            case_cap_usd=float(environ.get("DEMO_LIVE_CASE_CAP_USD", cls.case_cap_usd)),
            max_chars=int(environ.get("DEMO_LIVE_MAX_CHARS", cls.max_chars)),
            per_ip_per_hour=int(environ.get("DEMO_LIVE_PER_IP_PER_HOUR", cls.per_ip_per_hour)),
            trust_forwarded_for=_flag(environ.get("DEMO_LIVE_TRUST_FORWARDED_FOR")),
        )


class LiveRefused(Exception):
    """`status` is the HTTP status the route returns; `reason` a short code for the audit log."""

    def __init__(self, status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail


@dataclass
class RateLimiter:
    per_hour: int
    clock: Callable[[], float] = time.monotonic
    _seen: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        window = self._seen.setdefault(key, deque())
        while window and now - window[0] >= 3600:
            window.popleft()
        if len(window) >= self.per_hour:
            return False
        window.append(now)
        return True


def live_case(procedure_code: str, text: str, today: date) -> Case:
    """The visitor's text as a case: pages split on form feeds, at most MAX_PAGES."""
    chunks = [c.strip() for c in text.split(PAGE_BREAK) if c.strip()]
    if not chunks:
        raise LiveRefused(422, "empty", "the record is empty")
    if len(chunks) > MAX_PAGES:
        raise LiveRefused(422, "too_many_pages", f"at most {MAX_PAGES} pages")
    pages = tuple(Page(number=i, text=c) for i, c in enumerate(chunks, start=1))
    return Case(case_id="live", procedure_code=procedure_code, submitted_on=today, patient=LIVE_PATIENT, pages=pages)


class LiveRunner:
    """Runs one visitor record through the API's production system with a live client, under the caps above."""

    def __init__(
        self,
        config: LiveConfig,
        transport_factory: Callable[[], Transport] | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.config = config
        self.limiter = RateLimiter(config.per_ip_per_hour)
        self.today = today
        self._transport_factory = transport_factory
        self._day: date | None = None
        self._client: LLMClient | None = None

    def _transport(self) -> Transport:
        if self._transport_factory is not None:
            return self._transport_factory()
        from priorauth.llm.client import REQUEST_TIMEOUT_S, AnthropicTransport
        from priorauth.llm.resilience import ResilientTransport

        return ResilientTransport(AnthropicTransport(), max_attempts=2, timeout_s=REQUEST_TIMEOUT_S)

    def client(self) -> LLMClient:
        """One client per UTC day: its in-memory ledger is the daily cap."""
        day = self.today()
        if self._client is None or day != self._day:
            ledger = SpendLedger(path=None, cap_usd=self.config.daily_cap_usd)
            self._client = LLMClient(LLMMode.LIVE, CassetteStore(NO_CASSETTES), ledger, self._transport())
            self._day = day
        return self._client

    @property
    def spent_today_usd(self) -> float:
        if self._client is None or self._client.ledger is None or self._day != self.today():
            return 0.0
        return self._client.ledger.total_usd

    async def run(self, procedure_code: str, text: str, client_ip: str) -> tuple[Case, CaseRun]:
        if not self.config.enabled:
            raise LiveRefused(404, "disabled", "live mode is disabled on this demo; use the examples")
        if len(text) > self.config.max_chars:
            raise LiveRefused(413, "too_long", f"the record is limited to {self.config.max_chars} characters")
        if not self.limiter.allow(client_ip):
            raise LiveRefused(429, "rate_limited", f"at most {self.config.per_ip_per_hour} live runs per hour")
        if self.spent_today_usd >= self.config.daily_cap_usd:
            raise LiveRefused(503, "daily_cap", "today's live budget is spent; use the examples")
        case = live_case(procedure_code, text, self.today())
        llm = self.client()
        agent = AgentSystem("agent_v2_haiku", HAIKU, "agent@v2", self.config.case_cap_usd, llm=llm)
        system = ProtectedSystem(agent, name="agent_v2_haiku+protected (live)")
        try:
            run = await asyncio.wait_for(run_case(system, case), timeout=TIMEOUT_S)
        except TimeoutError:
            raise LiveRefused(504, "timeout", "the model did not answer in time; use the examples") from None
        if run.error:  # fail closed: no partial or fallback decision is shown
            kind = run.error.split(":", 1)[0]
            if kind == "BudgetExceeded":
                raise LiveRefused(503, "budget", "the live budget does not cover this record; use the examples")
            raise LiveRefused(502, kind[:40], "the model run failed; no decision was made; use the examples")
        return case, run
