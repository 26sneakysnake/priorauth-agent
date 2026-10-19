"""Baseline 2 (single call, JSON output) on a fake transport."""

import asyncio
import json
from pathlib import Path
from typing import Any

from eval.baselines.llm_single import SingleCallBaseline, output_schema
from priorauth.data.schema import Trap
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, SpendLedger
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import CaseRun, run_case
from priorauth.policy.engine import Outcome
from tests.test_agent import first_case


class Answer:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.text, self.stop_reason = text, stop_reason
        self.requests: list[dict[str, Any]] = []

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": self.text}],
            "stop_reason": self.stop_reason,
            "usage": {"input_tokens": 3000, "output_tokens": 800},
        }


def run(tmp_path: Path, answer: Answer, trap: Trap = Trap.CLEAN_APPROVE) -> CaseRun:
    case, _ = first_case(trap)
    client = LLMClient(LLMMode.LIVE, CassetteStore(tmp_path / "c"), SpendLedger(tmp_path / "s.json"), answer)
    return asyncio.run(run_case(SingleCallBaseline("single", HAIKU, "single@v1", 0.1, llm=client), case))


def test_json_answer_goes_through_verifier_and_engine(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    findings = [
        {
            "criterion_id": cid,
            "status": gc.status.value,
            "evidence": [e.model_dump() for e in gc.evidence],
            "conflict": False,
            "note": "",
        }
        for cid, gc in gold.criteria.items()
    ]
    answer = Answer(json.dumps({"findings": findings, "injection_detected": False, "injection_evidence": []}))
    result = run(tmp_path, answer)
    assert result.error is None and result.decision.outcome is Outcome.APPROVE
    request = answer.requests[0]
    assert "tools" not in request
    assert request["output_config"]["format"]["schema"] == output_schema(tuple(gold.criteria))
    assert all(f'<page number="{p.number}">' in request["messages"][0]["content"] for p in case.pages)
    assert result.usage.llm_calls == 1 and result.usage.cost_usd > 0


def test_unparseable_or_truncated_answer_pends(tmp_path: Path) -> None:
    bad = run(tmp_path, Answer("{not json"))
    assert bad.decision.outcome is Outcome.PEND and bad.error is not None and bad.error.startswith("BadOutput")
    assert bad.usage.llm_calls == 1  # the call is still counted
    cut = run(tmp_path, Answer("{}", stop_reason="max_tokens"))
    assert cut.error == "BadStop: stop_reason max_tokens"
