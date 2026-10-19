"""One case through the whole decision path: system under test -> verifier -> policy engine.

Any exception raised by the system fails closed: the case gets no findings and the engine pends it. A system that
gives up on a case raises ExtractionFailed, which carries what was spent so the cost metrics still count it.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from priorauth.agent.verifier import VerificationReport, verify
from priorauth.data.schema import Case
from priorauth.policy.engine import Decision, decide
from priorauth.policy.models import CaseFindings


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm_calls: int = 0
    input_tokens: int = 0  # uncached prompt tokens (API semantics)
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None  # None for systems without model calls


class Extraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: CaseFindings
    usage: Usage = Usage()


class ExtractionFailed(Exception):
    """A system gave up on a case (loop without progress, budget, refusal...). `kind` names the cause."""

    def __init__(self, kind: str, detail: str, usage: Usage) -> None:
        super().__init__(detail)
        self.kind = kind
        self.usage = usage


class System(Protocol):
    """A system under test reads a case and returns findings; it never decides."""

    name: str

    async def extract(self, case: Case) -> Extraction: ...


class CaseRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    raw: CaseFindings
    verified: CaseFindings
    verification: VerificationReport
    decision: Decision
    usage: Usage
    error: str | None = None


async def run_case(system: System, case: Case) -> CaseRun:
    error: str | None = None
    try:
        extraction = await system.extract(case)
    except Exception as e:  # fail closed: no findings means PEND
        kind, usage = (e.kind, e.usage) if isinstance(e, ExtractionFailed) else (type(e).__name__, Usage())
        error = f"{kind}: {e}"
        extraction = Extraction(findings=CaseFindings(procedure_code=case.procedure_code, findings=()), usage=usage)
    verified, report = verify(case, extraction.findings)
    return CaseRun(
        case_id=case.case_id,
        raw=extraction.findings,
        verified=verified,
        verification=report,
        decision=decide(verified),
        usage=extraction.usage,
        error=error,
    )
