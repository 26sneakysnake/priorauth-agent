"""On-disk formats. A case (what a system may see) and its gold labels are separate files.

data/<split>/cases/<case_id>.json   -> Case        (input to every system under test)
data/<split>/gold/<case_id>.json    -> Gold        (ground truth, read only by the evaluation harness)
"""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from priorauth.policy.models import Evidence, Status


class Trap(StrEnum):
    CLEAN_APPROVE = "clean_approve"
    PARAPHRASE = "paraphrase"
    CLEAN_PEND = "clean_pend"
    DOSE_X4 = "dose_x4"
    LATERALITY_SWAP = "laterality_swap"
    DATE_CONFLICT = "date_conflict"
    CONTRADICTION = "contradiction"
    MISSING_EVIDENCE = "missing_evidence"
    INJECTION = "injection"
    FROM_REVIEW = "from_review"  # not generated: a case labelled by a human reviewer (eval/review_to_cases.py)


ExpectedDecision = Literal["APPROVE", "PEND"]


class Patient(BaseModel):
    """Synthetic demographics. Every value is generated; identifiers carry a SYN prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    birth_date: date
    sex: Literal["female", "male"]
    mrn: str
    member_id: str


class Page(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    text: str


class Case(BaseModel):
    """One prior-authorization request: the structured header plus the faxed record pages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    synthetic: Literal[True] = True
    procedure_code: str
    submitted_on: date
    patient: Patient
    pages: tuple[Page, ...]

    def page(self, number: int) -> Page:
        for p in self.pages:
            if p.number == number:
                return p
        raise KeyError(number)


class GoldCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Status
    evidence: tuple[Evidence, ...]
    rationale: str


class Gold(BaseModel):
    """Ground truth for one case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    procedure_code: str
    trap: Trap
    target_criterion: str | None
    expected_decision: ExpectedDecision
    criteria: dict[str, GoldCriterion]
    injection: bool
    injection_evidence: tuple[Evidence, ...]
    paraphrased: tuple[str, ...]
    page_kinds: tuple[str, ...]
