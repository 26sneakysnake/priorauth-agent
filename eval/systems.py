"""Registry of the systems the harness can evaluate."""

from collections.abc import Callable

from eval.baselines.rules import RulesBaseline
from priorauth.pipeline import System

SYSTEMS: dict[str, Callable[[], System]] = {
    "rules_baseline": RulesBaseline,
}

# Systems whose results can be regenerated with no network and no API key (checked in CI).
OFFLINE: frozenset[str] = frozenset({"rules_baseline"})


def get_system(name: str) -> System:
    try:
        return SYSTEMS[name]()
    except KeyError:
        raise SystemExit(f"unknown system {name!r}; known: {', '.join(sorted(SYSTEMS))}") from None
