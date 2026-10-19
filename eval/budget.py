"""Cost estimate of a run before it starts, so a recording that cannot fit the remaining budget never begins.

The estimate prices a per-case token profile at the system's model rates, times a safety margin. The profile comes,
in order of preference, from the system's own cassettes (any split), from the cassettes of the system named in
COST_PRIOR (same prompt, other model), or from the system's static prior (`estimate_case_usage`).
"""

from collections import defaultdict
from typing import Protocol, runtime_checkable

from priorauth.data.schema import Case
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.pricing import TokenUsage, cost_usd

MARGIN = 1.5


@runtime_checkable
class Estimable(Protocol):
    model: str

    def estimate_case_usage(self, case: Case) -> TokenUsage: ...


def recorded_profile(store: CassetteStore, system_name: str) -> TokenUsage | None:
    """Mean recorded usage per case for one system, or None if it has no cassettes."""
    per_case: defaultdict[str, TokenUsage] = defaultdict(TokenUsage)
    for i in store.scope_interactions(system_name + "/"):
        per_case[i.label] = per_case[i.label] + i.usage
    if not per_case:
        return None
    n = len(per_case)
    total = sum(per_case.values(), TokenUsage())
    return TokenUsage(
        input_tokens=total.input_tokens // n,
        cache_write_tokens=total.cache_write_tokens // n,
        cache_read_tokens=total.cache_read_tokens // n,
        output_tokens=total.output_tokens // n,
    )


def estimate_run_cost(
    system_name: str, system: Estimable, cases: list[Case], store: CassetteStore, prior: str | None
) -> tuple[float, str]:
    """(estimated USD for the run, where the profile came from)."""
    for source in (system_name, prior):
        if source and (profile := recorded_profile(store, source)) is not None:
            return MARGIN * len(cases) * cost_usd(system.model, profile), f"cassettes of {source}"
    total = sum(cost_usd(system.model, system.estimate_case_usage(c)) for c in cases)
    return MARGIN * total, "static prior"
