import asyncio
from pathlib import Path

from eval.baselines.rules import _sentence
from priorauth.data.schema import Case
from priorauth.pipeline import Extraction, run_case
from priorauth.policy.engine import Outcome, ReasonCode

CASE = Case.model_validate_json((Path(__file__).resolve().parents[1] / "data/dev/cases/dev-001.json").read_bytes())


class Exploding:
    name = "exploding"

    async def extract(self, case: Case) -> Extraction:
        raise TimeoutError("model did not answer")


def test_a_system_exception_fails_closed_to_pend() -> None:
    run = asyncio.run(run_case(Exploding(), CASE))
    assert run.decision.outcome is Outcome.PEND
    assert run.error == "TimeoutError: model did not answer"
    assert {r.code for r in run.decision.reasons} == {ReasonCode.MISSING_FINDING}


def test_rule_sentences_are_exact_substrings() -> None:
    line = "51-year-old man seen for follow-up. Low back pain for 9 weeks. Denies fever."
    start = line.index("9 weeks")
    sentence = _sentence(line, start, start + 7)
    assert sentence == "Low back pain for 9 weeks."
    assert sentence in line
