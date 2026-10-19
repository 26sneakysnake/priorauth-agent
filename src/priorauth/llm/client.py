"""Anthropic Messages API client with cassettes, a spend ledger and hard cost caps.

Modes (environment variable LLM_MODE, default `replay`):
    replay  serve every call from eval/cassettes/; a missing cassette is an error. No network, no API key.
    record  serve from cassettes when the exact request was already recorded, otherwise call the API and record it.
    live    always call the API; nothing is written to the cassettes.

Credentials and endpoint come only from this project's variables: PRIORAUTH_ANTHROPIC_API_KEY, and optionally
PRIORAUTH_ANTHROPIC_BASE_URL (default https://api.anthropic.com). Both are passed to the SDK explicitly, so the SDK
never falls back on ANTHROPIC_* variables; ANTHROPIC_CUSTOM_HEADERS and ANTHROPIC_LOG, which it would still read,
make the client refuse to start. The key is never logged, stored or put in a cassette.

Cost control. Every real call is priced from the usage the API returns (priorauth.llm.pricing) and added to the
committed ledger results/spend.json before anything else happens. Before a real call, its worst-case cost is
reserved against three caps: the phase cap (PHASE_CAP_USD, cumulated over every recorded run), the run cap and the
per-case cap; a call that could cross one of them is not sent. The per-case check uses the same numbers in replay,
so a replayed loop stops exactly where the recorded one stopped.

Sampling: no temperature is sent. claude-haiku-5-5 and claude-sonnet-5-5 reject any non-default sampling value
with a 400, so reproducibility comes from the cassettes, not from temperature 0.
"""

import json
import os
import time
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from priorauth.llm.cassette import CassetteStore, Interaction, canonical_json, request_key
from priorauth.llm.pricing import PRICES_SOURCE, TokenUsage, cost_usd, max_cost_usd, model_prices

PHASE_CAP_USD = 10.0  # whole-phase budget (the phase budget is 10 EUR; 10 USD is the stricter of the two)
DEFAULT_LEDGER = Path("results/spend.json")
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_KEY_VAR = "PRIORAUTH_ANTHROPIC_API_KEY"
BASE_URL_VAR = "PRIORAUTH_ANTHROPIC_BASE_URL"
REQUEST_TIMEOUT_S = 120.0
MAX_RETRIES = 3  # SDK retries on 408/409/429/5xx and connection errors, with backoff
CHARS_PER_TOKEN_LOW = 3  # conservative: real prompts here average about 4 characters per token


class LLMMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class LLMError(RuntimeError):
    pass


class CassetteMiss(LLMError):
    """Replay asked for a request that was never recorded (prompt, tools, model or data changed)."""


class BudgetExceeded(LLMError):
    def __init__(self, scope: str, message: str) -> None:
        super().__init__(f"{scope} budget: {message}")
        self.scope = scope  # "case", "run" or "phase"


class ConfigError(LLMError):
    pass


class LLMCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    response: dict[str, Any]
    usage: TokenUsage
    cost_usd: float
    latency_ms: float
    replayed: bool


class CaseMeter:
    """Running cost of one case, against its cap. Counts replayed calls too, so replay follows the recorded path."""

    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent_usd = 0.0


def estimate_prompt_tokens(request: dict[str, Any]) -> int:
    """Over-estimate of the prompt size, without a network call (count_tokens would need one)."""
    return len(canonical_json(request)) // CHARS_PER_TOKEN_LOW + 1


def usage_from_response(response: dict[str, Any]) -> TokenUsage:
    u = response.get("usage") or {}
    return TokenUsage(
        input_tokens=int(u.get("input_tokens") or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
        output_tokens=int(u.get("output_tokens") or 0),
    )


# ---------------------------------------------------------------------------------------------------------------
# Spend ledger
# ---------------------------------------------------------------------------------------------------------------


class Spend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: TokenUsage, cost: float) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd = round(self.cost_usd + cost, 8)


class LedgerFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    description: str = (
        "Every real API call of phase P3, priced from the usage returned by the API. Written by "
        "priorauth.llm.client after each call; never edited by hand."
    )
    prices_source: str = PRICES_SOURCE
    cap_usd: float = PHASE_CAP_USD
    total: Spend = Spend()
    by_scope: dict[str, dict[str, Spend]] = {}  # scope ("<system>/<split>") -> model -> spend


class SpendLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER, cap_usd: float = PHASE_CAP_USD) -> None:
        self.path = path
        self.data = LedgerFile.model_validate_json(path.read_bytes()) if path.exists() else LedgerFile()
        self.data.cap_usd = cap_usd

    @property
    def total_usd(self) -> float:
        return self.data.total.cost_usd

    @property
    def remaining_usd(self) -> float:
        return self.data.cap_usd - self.total_usd

    def add(self, scope: str, model: str, usage: TokenUsage, cost: float) -> None:
        self.data.total.add(usage, cost)
        self.data.by_scope.setdefault(scope, {}).setdefault(model, Spend()).add(usage, cost)
        self.data.by_scope = dict(sorted(self.data.by_scope.items()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data.model_dump(mode="json"), indent=2) + "\n", "utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------------------------------------------


class Transport(Protocol):
    async def send(self, request: dict[str, Any]) -> dict[str, Any]: ...


class AnthropicTransport:
    """The only place that touches the network and the API key."""

    def __init__(self) -> None:
        import anthropic  # imported here so that replay never needs the SDK's network stack

        for var in ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG"):
            if os.environ.get(var):
                raise ConfigError(f"{var} is set; unset it (the SDK would apply it to this project's requests)")
        api_key = os.environ.get(API_KEY_VAR)
        if not api_key:
            raise ConfigError(f"{API_KEY_VAR} is not set; it is required in record and live modes")
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=os.environ.get(BASE_URL_VAR) or DEFAULT_BASE_URL,
            timeout=REQUEST_TIMEOUT_S,
            max_retries=MAX_RETRIES,
        )

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        message = await self._client.messages.create(**request)
        body: dict[str, Any] = message.to_dict(mode="json")
        return body


# ---------------------------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------------------------


class LLMClient:
    def __init__(
        self,
        mode: LLMMode,
        cassettes: CassetteStore,
        ledger: SpendLedger | None = None,
        transport: Transport | None = None,
    ) -> None:
        if mode is not LLMMode.REPLAY and (ledger is None or transport is None):
            raise ConfigError(f"{mode} mode needs a spend ledger and a transport")
        self.mode = mode
        self.cassettes = cassettes
        self.ledger = ledger
        self.transport = transport
        self.scope = "adhoc"
        self.run_cap_usd: float | None = None
        self.run_spent_usd = 0.0
        self.run_calls = 0
        self._reserved_usd = 0.0

    @classmethod
    def from_env(cls) -> "LLMClient":
        try:
            mode = LLMMode(os.environ.get("LLM_MODE", LLMMode.REPLAY.value))
        except ValueError:
            raise ConfigError(f"LLM_MODE must be one of {[m.value for m in LLMMode]}") from None
        cassettes = CassetteStore()
        if mode is LLMMode.REPLAY:
            return cls(mode, cassettes)
        return cls(mode, cassettes, SpendLedger(), AnthropicTransport())

    def begin_run(self, scope: str, cap_usd: float | None) -> None:
        """Name the run (cassette file and ledger entry) and set its cap; resets the run counters."""
        self.cassettes.path(scope)  # validates the scope
        self.scope = scope
        self.run_cap_usd = cap_usd
        self.run_spent_usd = 0.0
        self.run_calls = 0

    def end_run(self) -> None:
        self.cassettes.compact()

    async def create(self, request: dict[str, Any], *, meter: CaseMeter, label: str, step: int) -> LLMCall:
        model = request["model"]
        model_prices(model)  # an unpriced model is refused in every mode
        estimate = max_cost_usd(model, estimate_prompt_tokens(request), int(request["max_tokens"]))
        if meter.spent_usd + estimate > meter.cap_usd:
            raise BudgetExceeded(
                "case", f"spent {meter.spent_usd:.4f} + next call up to {estimate:.4f} > cap {meter.cap_usd:.4f} USD"
            )
        key = request_key(request)
        if self.mode is not LLMMode.LIVE and (hit := self.cassettes.get(key)) is not None:
            if self.mode is LLMMode.RECORD:
                self.cassettes.ensure_in(self.scope, hit)
            meter.spent_usd += hit.cost_usd
            return LLMCall(
                response=hit.response, usage=hit.usage, cost_usd=hit.cost_usd, latency_ms=hit.latency_ms, replayed=True
            )
        if self.mode is LLMMode.REPLAY:
            raise CassetteMiss(f"no cassette for {label} step {step} (key {key[:16]}); re-record with LLM_MODE=record")
        return await self._call_api(request, key, model, estimate, meter, label, step)

    async def _call_api(
        self, request: dict[str, Any], key: str, model: str, estimate: float, meter: CaseMeter, label: str, step: int
    ) -> LLMCall:
        if self.ledger is None or self.transport is None:  # excluded by __init__; keeps the type checker informed
            raise ConfigError("no ledger or transport")
        if self.ledger.total_usd + self._reserved_usd + estimate > self.ledger.data.cap_usd:
            raise BudgetExceeded(
                "phase",
                f"ledger {self.ledger.total_usd:.4f} + in flight {self._reserved_usd:.4f} + next call up to "
                f"{estimate:.4f} > cap {self.ledger.data.cap_usd:.2f} USD",
            )
        if self.run_cap_usd is not None and self.run_spent_usd + self._reserved_usd + estimate > self.run_cap_usd:
            raise BudgetExceeded(
                "run", f"run spent {self.run_spent_usd:.4f} + next call up to {estimate:.4f} > {self.run_cap_usd} USD"
            )
        self._reserved_usd += estimate
        try:
            start = time.monotonic()
            response = await self.transport.send(request)
            latency_ms = round((time.monotonic() - start) * 1000, 1)
        finally:
            self._reserved_usd -= estimate
        usage = usage_from_response(response)
        cost = round(cost_usd(model, usage), 8)  # the value stored in the cassette, so record and replay agree
        self.ledger.add(self.scope, model, usage, cost)
        self.run_spent_usd += cost
        self.run_calls += 1
        meter.spent_usd += cost
        if self.mode is LLMMode.RECORD:
            self.cassettes.put(
                self.scope,
                Interaction(
                    request_sha256=key,
                    label=label,
                    step=step,
                    model=model,
                    response=response,
                    usage=usage,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                ),
            )
        return LLMCall(response=response, usage=usage, cost_usd=cost, latency_ms=latency_ms, replayed=False)


@cache
def default_client() -> LLMClient:
    """One client per process, configured from the environment (systems in the registry share it)."""
    return LLMClient.from_env()
