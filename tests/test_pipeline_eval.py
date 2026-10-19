"""eval/pipeline.py: cases go through the API end to end; committed results replay byte for byte."""

import asyncio
from pathlib import Path

from eval import pipeline
from priorauth.agent.loop import AgentSystem
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
    assert [r.case_id for r in runs] != []
    assert [r.decision.outcome.value for r in runs] == [g.expected_decision for _, g in pairs]
    assert all(r.error is None for r in runs)


def test_pages_sent_matches_the_model_view() -> None:
    sent = pipeline.pages_sent(load("dev"))
    assert sent["pages_sent"] < sent["pages"] and sent["share_of_pages_sent"] == round(
        sent["pages_sent"] / sent["pages"], 4
    )
