"""What the model is allowed to see of a case, and the way back.

`model_view(case)` keeps only the pages that minimum necessary selects, renumbers them 1..k (the agent's tools
expect contiguous pages) and redacts every page. The structured patient header is replaced by placeholders too; the
prompts never send it, but a system that did would get nothing identifying from it.

`ModelView.restore(findings)` maps the model's citations back to the original record: page numbers to the original
pages, placeholders in quotes and notes to the original spellings. The deterministic verifier then checks every quote
against the ORIGINAL page, so redaction cannot make an unverifiable quote pass.

`ProtectedSystem` wraps any system under test with this view. The API's production system is wrapped; the
evaluation systems of P3 are not (their recorded cassettes hold the unredacted prompts they were measured with).
"""

from dataclasses import dataclass, field
from datetime import date

from priorauth.data.schema import Case, Page, Patient
from priorauth.pipeline import Extraction, System
from priorauth.policy.models import CaseFindings, Evidence
from priorauth.security.minimum_necessary import select_pages
from priorauth.security.redaction import Redactor

# The view's Patient needs a date; it is a fixed sentinel, never the patient's.
REDACTED_PATIENT_DATE = date(1900, 1, 1)


@dataclass(frozen=True)
class ModelView:
    case: Case  # what the model sees
    page_map: tuple[int, ...]  # view page i + 1 -> original page number
    n_original: int
    redactor: Redactor = field(repr=False)

    @property
    def share_sent(self) -> float:
        return len(self.page_map) / self.n_original

    def _evidence(self, ev: Evidence) -> Evidence:
        # A page outside the view stays outside the original record, so the verifier rejects it as out of range.
        page = self.page_map[ev.page - 1] if ev.page <= len(self.page_map) else self.n_original + ev.page
        return Evidence(page=page, quote=self.redactor.restore(ev.quote))

    def restore(self, findings: CaseFindings) -> CaseFindings:
        restored = tuple(
            f.model_copy(
                update={"evidence": tuple(map(self._evidence, f.evidence)), "note": self.redactor.restore(f.note)}
            )
            for f in findings.findings
        )
        return findings.model_copy(
            update={"findings": restored, "injection_evidence": tuple(map(self._evidence, findings.injection_evidence))}
        )


def model_view(case: Case) -> ModelView:
    redactor = Redactor(case.patient)
    selected = select_pages(case)
    pages = tuple(Page(number=i, text=redactor.apply(case.page(n).text)) for i, n in enumerate(selected, start=1))
    patient = Patient(
        name="[PATIENT]",
        birth_date=REDACTED_PATIENT_DATE,
        sex=case.patient.sex,
        mrn="[MRN]",
        member_id="[MEMBER_ID]",
    )
    view = Case(
        case_id=case.case_id,
        procedure_code=case.procedure_code,
        submitted_on=case.submitted_on,
        patient=patient,
        pages=pages,
    )
    return ModelView(case=view, page_map=selected, n_original=len(case.pages), redactor=redactor)


@dataclass
class ProtectedSystem:
    """A system under test that only ever receives the model view of a case (see the module docstring)."""

    inner: System
    name: str = ""

    def __post_init__(self) -> None:
        self.name = self.name or f"{self.inner.name}+protected"

    async def extract(self, case: Case) -> Extraction:
        view = model_view(case)
        extraction = await self.inner.extract(view.case)
        return extraction.model_copy(update={"findings": view.restore(extraction.findings)})
