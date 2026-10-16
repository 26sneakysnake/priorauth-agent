"""Types shared by the policy, the data generator, the systems under test and the engine."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Status(StrEnum):
    """Status of one policy criterion for one case.

    MET: the record shows the criterion is satisfied.
    NOT_MET: the record shows the criterion is not satisfied.
    INSUFFICIENT: the record is silent on the criterion, or contradicts itself about it.
    """

    MET = "MET"
    NOT_MET = "NOT_MET"
    INSUFFICIENT = "INSUFFICIENT"


class Evidence(BaseModel):
    """A citation: an exact quote from one page of the case record (pages are 1-based)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int = Field(ge=1)
    quote: str = Field(min_length=1)


class Finding(BaseModel):
    """What a system under test asserts about one criterion.

    `verified` is set only by the deterministic verifier (`priorauth.agent.verifier`); whatever a system puts there
    is overwritten. `conflict` lets a system say the record disagrees with itself on this criterion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str
    status: Status
    evidence: tuple[Evidence, ...] = ()
    conflict: bool = False
    verified: bool = False
    note: str = ""


class CaseFindings(BaseModel):
    """Everything the policy engine is allowed to look at for one case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    procedure_code: str
    findings: tuple[Finding, ...]
    injection_detected: bool = False
    injection_evidence: tuple[Evidence, ...] = ()
