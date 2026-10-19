"""Manual tool-use loop: the model reads the record through tools and records findings; it never decides.

One LLM call per step, at most `max_steps` steps. Every tool call of a step is executed (finish last) and all results
go back in one user message. The loop stops when finish succeeds. It gives up, and the case is pended with the reason,
when the step limit is reached, when two consecutive steps bring nothing new, when the model refuses, or when the
budget would be exceeded. The decision itself is always taken afterwards by priorauth.policy.engine.

The conversation is append-only and replays the model's content blocks unchanged (thinking signatures included), as
the API requires; the system prompt and tool list never change during a case, so they stay in the prompt cache.
"""

from dataclasses import dataclass
from typing import Any

from priorauth.agent.prompts import Prompt, load_prompt
from priorauth.agent.tools import TOOL_SPECS, AgentState, ToolOutcome
from priorauth.data.schema import Case
from priorauth.llm.cassette import canonical_json
from priorauth.llm.client import CaseMeter, LLMCall, LLMClient, default_client
from priorauth.llm.pricing import TokenUsage
from priorauth.obs.tracing import record_call, set_attrs, span
from priorauth.pipeline import Extraction, ExtractionFailed, Usage

TOOL_NAMES = frozenset(t["name"] for t in TOOL_SPECS)
MAX_STEPS = 12
MAX_IDLE_STEPS = 2  # consecutive steps without progress before giving up
NUDGE = "You stopped without calling a tool. Record any missing findings with record_finding, then call finish."

# Keys of each response content block that are sent back to the API; anything else is response-only metadata.
_BLOCK_KEYS = {
    "text": ("type", "text"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
    "tool_use": ("type", "id", "name", "input"),
}


def assistant_content(response: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = []
    for block in response.get("content") or []:
        keys = _BLOCK_KEYS.get(block.get("type"))
        if keys is None:
            blocks.append({k: v for k, v in block.items() if v is not None})
        else:
            blocks.append({k: block[k] for k in keys if k in block})
    return blocks


def build_request(model: str, prompt: Prompt, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": prompt.max_tokens,
        # Breakpoint 1: tools + system, shared by every case. Breakpoint 2 (top level, automatic): the conversation
        # so far, so that each step reads the previous step's prefix from the cache.
        "system": [{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}],
        "tools": TOOL_SPECS,
        "messages": messages,
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": prompt.effort},
        "cache_control": {"type": "ephemeral"},
    }


class _Tally:
    def __init__(self) -> None:
        self.usage = Usage(latency_ms=0.0)

    def add(self, call: LLMCall) -> None:
        u, c = self.usage, call.usage
        self.usage = Usage(
            llm_calls=u.llm_calls + 1,
            input_tokens=u.input_tokens + c.input_tokens,
            cache_write_tokens=u.cache_write_tokens + c.cache_write_tokens,
            cache_read_tokens=u.cache_read_tokens + c.cache_read_tokens,
            output_tokens=u.output_tokens + c.output_tokens,
            cost_usd=round(u.cost_usd + call.cost_usd, 8),
            latency_ms=round((u.latency_ms or 0.0) + call.latency_ms, 1),
        )


def _run_tools(state: AgentState, tool_uses: list[dict[str, Any]], step: int) -> tuple[list[dict[str, Any]], bool]:
    """Execute a step's tool calls, finish last; results keep the order of the calls."""
    order = sorted(range(len(tool_uses)), key=lambda i: tool_uses[i]["name"] == "finish")
    outcomes: dict[int, ToolOutcome] = {}
    for i in order:
        name = tool_uses[i]["name"]
        known = name if name in TOOL_NAMES else "unknown"  # a model-chosen string never reaches a span as is
        with span("tool.call", step=step, tool__name=known) as s:
            outcomes[i] = state.execute(name, tool_uses[i].get("input"))
            set_attrs(s, tool__is_error=outcomes[i].is_error, tool__progress=outcomes[i].progress)
    results = []
    for i, block in enumerate(tool_uses):
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block["id"], "content": outcomes[i].content}
        if outcomes[i].is_error:
            result["is_error"] = True
        results.append(result)
    return results, any(o.progress for o in outcomes.values())


async def run_agent(
    case: Case, *, model: str, prompt: Prompt, llm: LLMClient, case_cap_usd: float, max_steps: int = MAX_STEPS
) -> Extraction:
    state = AgentState(case)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt.render_task(case)}]
    meter = CaseMeter(case_cap_usd)
    tally = _Tally()
    idle = 0

    def stop(kind: str, detail: str) -> ExtractionFailed:
        return ExtractionFailed(kind, detail, tally.usage)

    for step in range(max_steps):
        try:
            with span("llm.call", step=step, llm__model=model) as s:
                call = await llm.create(
                    build_request(model, prompt, messages), meter=meter, label=case.case_id, step=step
                )
                record_call(s, call)
        except Exception as e:
            raise stop(type(e).__name__, str(e)) from e
        tally.add(call)
        stop_reason = call.response.get("stop_reason")
        if stop_reason == "refusal":
            raise stop("Refusal", f"model refused at step {step}")
        content = assistant_content(call.response)
        messages.append({"role": "assistant", "content": content})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if tool_uses:
            results, progress = _run_tools(state, tool_uses, step)
            if state.finished:
                return Extraction(findings=state.case_findings(), usage=tally.usage)
            messages.append({"role": "user", "content": results})
        else:
            progress = False
            messages.append({"role": "user", "content": NUDGE})
        idle = 0 if progress else idle + 1
        if idle >= MAX_IDLE_STEPS:
            raise stop("AgentStopped", f"no progress in {idle} consecutive steps (last stop_reason {stop_reason})")
    raise stop("AgentStopped", f"max_steps={max_steps} reached without finish")


@dataclass
class AgentSystem:
    """The agent as a system under test (see priorauth.pipeline.System)."""

    name: str
    model: str
    prompt_id: str
    case_cap_usd: float
    llm: LLMClient | None = None  # None: the process-wide client configured from the environment

    def __post_init__(self) -> None:
        self.prompt = load_prompt(self.prompt_id)

    @property
    def client(self) -> LLMClient:
        return self.llm or default_client()

    async def extract(self, case: Case) -> Extraction:
        return await run_agent(
            case, model=self.model, prompt=self.prompt, llm=self.client, case_cap_usd=self.case_cap_usd
        )

    def estimate_case_usage(self, case: Case) -> TokenUsage:
        """Prior for the cost estimate before a first recording: six steps that each resend the system prompt,
        the tools and every page uncached, and write 1,500 output tokens."""
        steps = 6
        chars = len(self.prompt.system) + len(canonical_json(TOOL_SPECS)) + sum(len(p.text) for p in case.pages)
        return TokenUsage(input_tokens=steps * (chars // 3 + 500), output_tokens=steps * 1500)
