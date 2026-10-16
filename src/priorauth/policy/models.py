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
