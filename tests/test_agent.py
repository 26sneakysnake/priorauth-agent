"""Agent tools and loop, driven by a scripted model (no network, no key)."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from priorauth.agent import loop
from priorauth.agent.prompts import PROMPTS_DIR, load_prompt
from priorauth.agent.tools import ARGS, TOOL_SPECS, AgentState
from priorauth.data.schema import Case, Gold, Trap
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, SpendLedger
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import CaseRun, run_case
from priorauth.policy.engine import Outcome, ReasonCode

DATA = Path(__file__).resolve().parents[1] / "data" / "dev"


def first_case(trap: Trap) -> tuple[Case, Gold]:
    for path in sorted((DATA / "gold").glob("*.json")):
        gold = Gold.model_validate_json(path.read_bytes())
        if gold.trap is trap:
            return Case.model_validate_json((DATA / "cases" / path.name).read_bytes()), gold
    raise LookupError(trap)


Turn = list[tuple[str, dict[str, Any]]]  # tool calls of one assistant turn; [] means a text-only turn


class Script:
    """A fake model: returns one scripted assistant turn per call and keeps every request it received."""

    def __init__(self, turns: list[Turn], stop_reason: str = "tool_use") -> None:
        self.turns = turns
        self.stop_reason = stop_reason
        self.requests: list[dict[str, Any]] = []

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        n = len(self.requests)
        turn = self.turns[min(n, len(self.turns)) - 1]
        content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": f"sig{n}"}]
        content += [
            {"type": "tool_use", "id": f"t{n}_{i}", "name": name, "input": args, "caller": {"type": "direct"}}
            for i, (name, args) in enumerate(turn)
        ]
        if not turn:
            content.append({"type": "text", "text": "Done.", "citations": None})
        return {
            "id": f"msg_{n}",
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": content,
            "stop_reason": self.stop_reason if turn else "end_turn",
            "usage": {"input_tokens": 500, "cache_read_input_tokens": 1000, "output_tokens": 200},
        }


def system(tmp_path: Path, script: Script, cap: float = 0.10) -> loop.AgentSystem:
    client = LLMClient(LLMMode.LIVE, CassetteStore(tmp_path / "c"), SpendLedger(tmp_path / "spend.json"), script)
    return loop.AgentSystem("agent_test", HAIKU, "agent@v1", cap, llm=client)


def run(tmp_path: Path, case: Case, script: Script, cap: float = 0.10) -> CaseRun:
    return asyncio.run(run_case(system(tmp_path, script, cap), case))


def gold_findings(gold: Gold, edit: Callable[[str, dict[str, Any]], None] = lambda cid, a: None) -> Turn:
    calls = []
    for cid, gc in gold.criteria.items():
        args: dict[str, Any] = {
            "criterion_id": cid,
            "status": gc.status.value,
            "evidence": [{"page": e.page, "quote": e.quote} for e in gc.evidence],
        }
        edit(cid, args)
        calls.append(("record_finding", args))
    return calls


def reading(case: Case) -> list[Turn]:
    return [
        [("list_pages", {})],
        [("read_page", {"page": p.number}) for p in case.pages],
    ]


def test_agent_reads_records_and_the_engine_approves_a_clean_case(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    policy = [("search_policy", {"criterion_id": cid}) for cid in gold.criteria]
    script = Script([*reading(case), policy, [*gold_findings(gold), ("finish", {"injection_detected": False})]])
    result = run(tmp_path, case, script)
    assert result.error is None
    assert result.decision.outcome is Outcome.APPROVE
    assert result.verification.n_valid == result.verification.n_quotes > 0
    assert result.usage.llm_calls == 4
    assert result.usage.cost_usd > 0 and result.usage.latency_ms is not None


def test_conversation_is_append_only_and_replays_blocks_unchanged(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    script = Script([*reading(case), [*gold_findings(gold), ("finish", {"injection_detected": False})]])
    run(tmp_path, case, script)
    for before, after in zip(script.requests, script.requests[1:], strict=False):
        assert after["messages"][: len(before["messages"])] == before["messages"]
        assert (after["system"], after["tools"]) == (before["system"], before["tools"])
    second = script.requests[1]["messages"][1]["content"]
    assert second[0] == {"type": "thinking", "thinking": "", "signature": "sig1"}
    assert second[1] == {"type": "tool_use", "id": "t1_0", "name": "list_pages", "input": {}}  # no response-only keys
    assert all("temperature" not in r for r in script.requests)


def test_bad_arguments_come_back_as_tool_errors(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    bad: Turn = [
        ("read_page", {"page": 99}),
        ("read_page", {"page": "2"}),
        ("search_policy", {"criterion_id": "NOPE"}),
        ("record_finding", {"criterion_id": next(iter(gold.criteria)), "status": "MET", "evidence": []}),
        ("delete_record", {}),
    ]
    script = Script([bad, [*gold_findings(gold), ("finish", {"injection_detected": False})]])
    run(tmp_path, case, script)
    results = script.requests[1]["messages"][2]["content"]
    assert [r.get("is_error") for r in results] == [True] * 5
    texts = [r["content"] for r in results]
    assert f"page 99 does not exist; this record has pages 1 to {len(case.pages)}" in texts[0]
    assert "page: Input should be a valid integer" in texts[1]
    assert "unknown criterion 'NOPE'" in texts[2]
    assert "needs at least one evidence quote" in texts[3]
    assert "unknown tool 'delete_record'" in texts[4]


def test_loop_without_progress_stops_and_pends(tmp_path: Path) -> None:
    case, _ = first_case(Trap.CLEAN_APPROVE)
    script = Script([[("read_page", {"page": 1})]])  # the same page, over and over
    result = run(tmp_path, case, script)
    assert result.decision.outcome is Outcome.PEND
    assert result.error is not None and result.error.startswith("AgentStopped: no progress in 2 consecutive steps")
    assert len(script.requests) == 3  # one useful read, then two idle steps
    assert result.usage.llm_calls == 3  # what was spent is still counted


def test_text_only_turns_are_nudged_then_stopped(tmp_path: Path) -> None:
    case, _ = first_case(Trap.CLEAN_APPROVE)
    script = Script([[]])
    result = run(tmp_path, case, script)
    assert result.error is not None and result.error.startswith("AgentStopped")
    assert script.requests[1]["messages"][-1] == {"role": "user", "content": loop.NUDGE}


def test_step_limit_pends(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    cid = next(iter(gold.criteria))
    # Each turn changes a finding, so it counts as progress, but the model never calls finish.
    turns: list[Turn] = [
        [("record_finding", {"criterion_id": cid, "status": "INSUFFICIENT", "evidence": [], "note": str(i)})]
        for i in range(20)
    ]
    script = Script(turns)
    result = run(tmp_path, case, script)
    assert result.error == f"AgentStopped: max_steps={loop.MAX_STEPS} reached without finish"
    assert result.decision.outcome is Outcome.PEND
    assert len(script.requests) == loop.MAX_STEPS


def test_refusal_pends(tmp_path: Path) -> None:
    case, _ = first_case(Trap.CLEAN_APPROVE)
    result = run(tmp_path, case, Script([[("list_pages", {})]], stop_reason="refusal"))
    assert result.error == "Refusal: model refused at step 0"
    assert result.decision.outcome is Outcome.PEND


def test_case_budget_pends_before_the_call(tmp_path: Path) -> None:
    case, _ = first_case(Trap.CLEAN_APPROVE)
    script = Script([[("list_pages", {})]])
    result = run(tmp_path, case, script, cap=0.0001)
    assert result.error is not None and result.error.startswith("BudgetExceeded: case budget")
    assert script.requests == [] and result.decision.outcome is Outcome.PEND


def test_finish_with_missing_findings_is_refused_once(tmp_path: Path) -> None:
    case, gold = first_case(Trap.CLEAN_APPROVE)
    partial = gold_findings(gold)[:2]
    script = Script([[*partial, ("finish", {"injection_detected": False})], [("finish", {"injection_detected": False})]])
    result = run(tmp_path, case, script)
    first_results = script.requests[1]["messages"][2]["content"]
    assert first_results[-1]["is_error"] and "no finding recorded yet" in first_results[-1]["content"]
    assert result.error is None
    assert ReasonCode.MISSING_FINDING in {r.code for r in result.decision.reasons}
    assert result.decision.outcome is Outcome.PEND


def test_flagged_injection_pends_an_otherwise_approvable_case(tmp_path: Path) -> None:
    case, gold = first_case(Trap.INJECTION)
    ev = gold.injection_evidence[0]
    finish = ("finish", {"injection_detected": True, "injection_evidence": [{"page": ev.page, "quote": ev.quote}]})
    result = run(tmp_path, case, Script([[*gold_findings(gold), finish]]))
    assert result.decision.outcome is Outcome.PEND
    assert ReasonCode.INJECTION_DETECTED in {r.code for r in result.decision.reasons}


def test_tool_specs_match_the_validated_arguments() -> None:
    assert [t["name"] for t in TOOL_SPECS] == list(ARGS)
    for spec in TOOL_SPECS:
        model = ARGS[spec["name"]]
        assert set(spec["input_schema"]["properties"]) == set(model.model_fields)
        required = {n for n, f in model.model_fields.items() if f.is_required()}
        assert required <= set(spec["input_schema"].get("required", []))


def test_state_lists_pages_with_their_headings() -> None:
    case, _ = first_case(Trap.CLEAN_APPROVE)
    text = AgentState(case).execute("list_pages", {}).content
    assert text.startswith(f"The record has {len(case.pages)} pages.")
    assert all(f"Page {p.number}: " in text for p in case.pages)


@pytest.mark.parametrize("path", sorted(PROMPTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_prompts_load_and_name_every_criterion(path: Path) -> None:
    prompt = load_prompt(path.stem)
    case, gold = first_case(Trap.CLEAN_APPROVE)
    task = prompt.render_task(case)
    assert all(cid in task for cid in gold.criteria)
    assert case.case_id in task and "{" not in task
