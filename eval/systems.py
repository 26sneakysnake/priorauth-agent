"""Registry of the systems the harness can evaluate."""

from collections.abc import Callable

from eval.baselines.llm_single import SingleCallBaseline
from eval.baselines.rules import RulesBaseline
from priorauth.agent.loop import AgentSystem
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import System

# Per-case hard caps (USD). Far above what a normal case costs; they stop a runaway loop, not a normal one.
HAIKU_CASE_CAP_USD = 0.10
SONNET_CASE_CAP_USD = 1.00

SYSTEMS: dict[str, Callable[[], System]] = {
    "rules_baseline": RulesBaseline,
    "llm_single_haiku": lambda: SingleCallBaseline("llm_single_haiku", HAIKU, "single@v1", HAIKU_CASE_CAP_USD),
    "agent_v1_haiku": lambda: AgentSystem("agent_v1_haiku", HAIKU, "agent@v1", HAIKU_CASE_CAP_USD),
    "agent_v2_haiku": lambda: AgentSystem("agent_v2_haiku", HAIKU, "agent@v2", HAIKU_CASE_CAP_USD),
    # Prompt-sensitivity probe: a sloppy "lean toward MET" prompt (dev only).
    "agent_lenient_haiku": lambda: AgentSystem("agent_lenient_haiku", HAIKU, "agent@lenient", HAIKU_CASE_CAP_USD),
    # Test fixture for the gate (tests/test_gate.py): a deliberately broken prompt, replayed on dev only.
    "agent_degraded_haiku": lambda: AgentSystem("agent_degraded_haiku", HAIKU, "agent@degraded", HAIKU_CASE_CAP_USD),
}

# Systems whose results can be regenerated with no network and no API key (checked in CI), with the splits whose
# results are committed. LLM systems replay their recorded cassettes.
OFFLINE: dict[str, tuple[str, ...]] = {
    "rules_baseline": ("dev", "test", "robust"),
    "llm_single_haiku": ("dev", "test", "robust"),
    "agent_v1_haiku": ("dev", "test", "robust"),
    "agent_v2_haiku": ("dev", "test", "robust"),
    "agent_lenient_haiku": ("dev",),
    "agent_degraded_haiku": ("dev",),
}

# System whose recorded token usage serves as the prior when estimating the cost of a system's first recording.
COST_PRIOR: dict[str, str] = {"agent_v2_haiku": "agent_v1_haiku", "agent_degraded_haiku": "agent_v1_haiku"}


def get_system(name: str) -> System:
    try:
        return SYSTEMS[name]()
    except KeyError:
        raise SystemExit(f"unknown system {name!r}; known: {', '.join(sorted(SYSTEMS))}") from None
