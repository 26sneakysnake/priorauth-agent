"""The load-test script runs end to end and its deterministic section is reproducible (small configuration)."""

import asyncio
from pathlib import Path

from eval import load_test

DATA = Path(__file__).resolve().parents[1] / "data"


def test_load_test_is_reproducible() -> None:
    first = asyncio.run(load_test.run(20, workers=8, latency_s=0.0, jitter_s=0.01, seed=1, data=DATA))
    second = asyncio.run(load_test.run(20, workers=8, latency_s=0.0, jitter_s=0.01, seed=1, data=DATA))
    assert first["deterministic"] == second["deterministic"]
    d = first["deterministic"]
    assert d["statuses"] == {"done": 20} and d["matches_expected_decision"] == 20 and d["llm_calls"] == 40
    assert first["llm"]["simulated"] is True
    assert first["timing"]["peak_concurrent_llm_calls"] <= 8


def test_committed_load_test_says_the_model_is_simulated() -> None:
    import json

    report = json.loads((DATA.parent / "results/load_test.json").read_text())
    assert report["llm"]["simulated"] is True and "SIMULATED" in report["description"]
    assert report["deterministic"]["statuses"] == {"done": report["settings"]["cases"]}
    assert {"platform", "cpu_count", "memory_gb", "python"} <= set(report["machine"])
