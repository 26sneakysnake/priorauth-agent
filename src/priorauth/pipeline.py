"""One case through the whole decision path: system under test -> verifier -> policy engine.

Any exception raised by the system fails closed: the case gets no findings and the engine pends it.
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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None  # None for systems without model calls


class Extraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: CaseFindings
    usage: Usage = Usage()


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
        error = f"{type(e).__name__}: {e}"
        extraction = Extraction(findings=CaseFindings(procedure_code=case.procedure_code, findings=()))
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
