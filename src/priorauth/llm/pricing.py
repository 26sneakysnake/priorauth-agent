"""Token prices for the models this project calls, in USD per million tokens.

Source: https://platform.claude.com/docs/en/about-claude/pricing, read on 2026-10-19 (first-party Claude API,
global routing, standard tier, no batch discount). Only 5-minute cache writes are priced because the client never
asks for the 1-hour TTL. A model that is not in this table cannot be called: the client refuses rather than guess.
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

PRICES_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (read 2026-10-19)"

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Rates:
    input: float
    cache_write_5m: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class ModelPrices:
    standard: Rates
    # Some models bill a whole request at higher rates once its prompt (input + cache writes + cache reads) exceeds
    # a threshold; the threshold is in prompt tokens.
    long_prompt: Rates | None = None
    long_prompt_threshold: int | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        if self.long_prompt and self.long_prompt_threshold and prompt_tokens > self.long_prompt_threshold:
            return self.long_prompt
        return self.standard


PRICES: dict[str, ModelPrices] = {
    HAIKU: ModelPrices(
        standard=Rates(input=0.10, cache_write_5m=0.125, cache_read=0.01, output=0.50),
        long_prompt=Rates(input=0.50, cache_write_5m=0.625, cache_read=0.05, output=2.50),
        long_prompt_threshold=100_000,
    ),
    SONNET: ModelPrices(standard=Rates(input=2.00, cache_write_5m=2.50, cache_read=0.10, output=10.00)),
}


class TokenUsage(BaseModel):
    """Token counts as billed. `input_tokens` is the uncached part of the prompt only (API semantics)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class UnknownModelError(KeyError):
    pass


def model_prices(model: str) -> ModelPrices:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(f"no price for model {model!r}; add it to priorauth.llm.pricing first") from None


def cost_usd(model: str, usage: TokenUsage) -> float:
    """Cost of one request, from the usage the API returned."""
    r = model_prices(model).rates_for(usage.prompt_tokens)
    total = (
        usage.input_tokens * r.input
        + usage.cache_write_tokens * r.cache_write_5m
        + usage.cache_read_tokens * r.cache_read
        + usage.output_tokens * r.output
    )
    return total / 1_000_000


def max_cost_usd(model: str, prompt_tokens: int, max_output_tokens: int) -> float:
    """Upper bound for one request before it is sent: every prompt token billed as a cache write (the most expensive
    input category) and the full output budget used."""
    r = model_prices(model).rates_for(prompt_tokens)
    return (prompt_tokens * max(r.input, r.cache_write_5m) + max_output_tokens * r.output) / 1_000_000
