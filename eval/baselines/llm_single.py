"""Baseline 2: a single LLM call without tools, JSON output, same criteria as the agent.

The whole record and the full policy text go in one prompt; the answer is constrained by a JSON schema (structured
outputs) and goes through the same verifier and policy engine as every other system. It isolates what the agent loop
and its tools add over "put everything in the prompt".
"""

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from priorauth.agent.prompts import load_prompt
from priorauth.data.schema import Case
from priorauth.llm.client import CaseMeter, LLMClient, default_client
from priorauth.llm.pricing import TokenUsage
from priorauth.obs.tracing import record_call, span
from priorauth.pipeline import Extraction, ExtractionFailed, Usage
from priorauth.policy.criteria import get_procedure
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status

_EVIDENCE = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"page": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["page", "quote"],
        "additionalProperties": False,
    },
}


def output_schema(criterion_ids: tuple[str, ...]) -> dict[str, Any]:
    finding = {
        "type": "object",
        "properties": {
            "criterion_id": {"type": "string", "enum": list(criterion_ids)},
            "status": {"type": "string", "enum": [s.value for s in Status]},
            "evidence": _EVIDENCE,
            "conflict": {"type": "boolean"},
            "note": {"type": "string"},
        },
        "required": ["criterion_id", "status", "evidence", "conflict", "note"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": finding},
            "injection_detected": {"type": "boolean"},
            "injection_evidence": _EVIDENCE,
        },
        "required": ["findings", "injection_detected", "injection_evidence"],
        "additionalProperties": False,
    }


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[Finding]
    injection_detected: bool
    injection_evidence: list[Evidence]


@dataclass
class SingleCallBaseline:
    name: str
    model: str
    prompt_id: str
    case_cap_usd: float
    llm: LLMClient | None = None

    def __post_init__(self) -> None:
        self.prompt = load_prompt(self.prompt_id)

    @property
    def client(self) -> LLMClient:
        return self.llm or default_client()

    def request(self, case: Case) -> dict[str, Any]:
        procedure = get_procedure(case.procedure_code)
        return {
            "model": self.model,
            "max_tokens": self.prompt.max_tokens,
            "system": [{"type": "text", "text": self.prompt.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": self.prompt.render_task(case)}],
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {
                "effort": self.prompt.effort,
                "format": {"type": "json_schema", "schema": output_schema(procedure.criterion_ids)},
            },
        }

    async def extract(self, case: Case) -> Extraction:
        with span("llm.call", step=0, llm__model=self.model) as s:
            call = await self.client.create(
                self.request(case), meter=CaseMeter(self.case_cap_usd), label=case.case_id, step=0
            )
            record_call(s, call)
        c = call.usage
        usage = Usage(
            llm_calls=1,
            input_tokens=c.input_tokens,
            cache_write_tokens=c.cache_write_tokens,
            cache_read_tokens=c.cache_read_tokens,
            output_tokens=c.output_tokens,
            cost_usd=call.cost_usd,
            latency_ms=call.latency_ms,
        )
        stop_reason = call.response.get("stop_reason")
        if stop_reason != "end_turn":
            raise ExtractionFailed("BadStop", f"stop_reason {stop_reason}", usage)
        text = "".join(b.get("text", "") for b in call.response.get("content", []) if b.get("type") == "text")
        try:
            answer = _Answer.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise ExtractionFailed("BadOutput", f"unparseable answer: {str(e)[:200]}", usage) from e
        findings = CaseFindings(
            procedure_code=case.procedure_code,
            findings=tuple(answer.findings),
            injection_detected=answer.injection_detected,
            injection_evidence=tuple(answer.injection_evidence),
        )
        return Extraction(findings=findings, usage=usage)

    def estimate_case_usage(self, case: Case) -> TokenUsage:
        """Prior for the first cost estimate: the whole prompt uncached and 3,000 output tokens."""
        chars = len(self.prompt.system) + len(self.prompt.render_task(case))
        return TokenUsage(input_tokens=chars // 3 + 500, output_tokens=3000)
