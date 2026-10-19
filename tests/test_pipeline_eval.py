"""eval/pipeline.py: cases go through the API end to end; committed results replay byte for byte."""

import asyncio
from pathlib import Path

import pytest

from eval import pipeline, pipeline_report
from priorauth.agent.loop import AgentSystem
from priorauth.llm.client import LLMClient, LLMMode
from priorauth.llm.fake import fake_client, protected_oracle
from priorauth.llm.pricing import HAIKU
from priorauth.security.model_view import ProtectedSystem
from tests.test_data import load

ROOT = Path(__file__).resolve().parents[1]


def test_every_case_goes_through_the_api_and_comes_back_in_order() -> None:
    pairs = load("dev")[:12]
    oracle = protected_oracle(pairs)

    def system() -> ProtectedSystem:
        return ProtectedSystem(AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(oracle)))

    runs = asyncio.run(pipeline.through_api(pairs, system, workers=4))
    assert [r.case_id for r in runs] == [c.case_id for c, _ in pairs]  # paired with gold by the submitter's id
    assert [r.decision.outcome.value for r in runs] == [g.expected_decision for _, g in pairs]
    assert all(r.error is None for r in runs)


def test_pages_sent_matches_the_model_view() -> None:
    sent = pipeline.pages_sent(load("dev"))
    assert sent["pages_sent"] < sent["pages"] and sent["share_of_pages_sent"] == round(
        sent["pages_sent"] / sent["pages"], 4
    )


@pytest.mark.parametrize("split", ["dev", "test", "robust"])
def test_committed_pipeline_results_replay_byte_for_byte(split: str) -> None:
    """The real-model measurement replays from its cassettes through the API: same code + data + cassettes -> same
    bytes (no network, no key: LLM_MODE=replay, set by conftest)."""
    client = LLMClient.from_env()
    assert client.mode is LLMMode.REPLAY
    results, errors = pipeline.evaluate(split, ROOT / "data", client)
    assert results == (ROOT / f"results/pipeline_{split}.json").read_bytes()
    assert errors == (ROOT / f"results/pipeline_{split}_errors.jsonl").read_bytes()


def test_committed_comparison_table_is_up_to_date() -> None:
    assert pipeline_report.render(ROOT / "data") == (ROOT / "results/pipeline_comparison.md").read_text()
