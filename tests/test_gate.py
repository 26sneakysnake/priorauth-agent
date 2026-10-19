"""The gate passes on the production agent and fails on a deliberately degraded prompt (replayed, no API key)."""

import json
from pathlib import Path
from typing import Any

from eval import gate, run

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads((ROOT / gate.REFERENCE).read_bytes())


def replay(system: str) -> dict[str, Any]:
    results: dict[str, Any] = json.loads(run.evaluate(system, gate.GATE_SPLIT, ROOT / "data")[0])
    return results


def test_reference_is_the_committed_result_of_the_gated_system() -> None:
    results_path, _ = run.output_paths(ROOT / "results", gate.GATED_SYSTEM, gate.GATE_SPLIT)
    assert gate.reference_from(json.loads(results_path.read_bytes())) == REFERENCE


def test_gate_passes_on_the_production_agent() -> None:
    assert gate.check(replay(gate.GATED_SYSTEM), REFERENCE) == []


def test_gate_fails_on_a_degraded_prompt() -> None:
    failures = gate.check(replay("agent_degraded_haiku"), REFERENCE)
    assert any(f.startswith("dangerous approvals") for f in failures), failures


def test_gate_rules_on_hand_made_metrics() -> None:
    good = replay(gate.GATED_SYSTEM)
    m = good["metrics"]
    worse_accuracy = {**m, "criterion_accuracy": {**m["criterion_accuracy"], "value": 0.5}}
    injected = {**m, "by_trap": {**m["by_trap"], "injection": {**m["by_trap"]["injection"], "dangerous_approvals": 1}}}
    errors = {**m, "system_errors": m["system_errors"] + 1}
    assert gate.check({**good, "metrics": worse_accuracy}, REFERENCE)[0].startswith("criterion accuracy")
    assert gate.check({**good, "metrics": injected}, REFERENCE) == ["1 case(s) with a prompt injection approved"]
    assert gate.check({**good, "metrics": errors}, REFERENCE)[0].endswith("system errors > reference 0")
    assert gate.check({**good, "data_digest": "0" * 64}, REFERENCE)[0].startswith("data changed")


def test_gate_cli_exit_codes() -> None:
    assert gate.main([]) == 0
    assert gate.main(["--system", "agent_degraded_haiku"]) == 1
