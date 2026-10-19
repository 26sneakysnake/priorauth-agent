"""De-identification before any model call: identifiers replaced, exactly reversible, never sent to the model."""

import asyncio
import json
from datetime import date
from typing import Any

import pytest

from eval.minimum_necessary import residual_identifiers
from priorauth.agent.loop import AgentSystem
from priorauth.agent.verifier import verify
from priorauth.api.app import default_system
from priorauth.data.schema import Case, Page, Patient
from priorauth.llm.fake import OracleTransport, fake_client, protected_oracle
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import run_case
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status
from priorauth.security.model_view import ModelView, ProtectedSystem, model_view
from priorauth.security.redaction import Redactor, date_forms
from tests.test_data import load

SPLITS = {split: load(split) for split in ("dev", "test", "robust")}
PATIENT = Patient(
    name="Rosa Zielinski", birth_date=date(1959, 1, 4), sex="female", mrn="SYN-7835740", member_id="EHP-SYN-85756847"
)
TEXT = (
    "Member: Rosa Zielinski | Member ID: EHP-SYN-85756847 | DOB: 01/04/1959\n"
    "Patient: ROSA ZIELINSKI | MRN: SYN-7835740 | D.O.B. January 4, 1959 | born 1959-01-04 or 01-04-1959\n"
    "Ms. Zielinski called back; callback number (312) 555-0138, alternate 773-555-0199.\n"
    "Pain 4/10 since 03/02/2025. Rosalind Zielinskiova is a different person."
)


def leaks(patient: Patient, text: str) -> int:
    """Occurrences of the patient's known identifiers (whole words, any case, bare MRN and member digits too)."""
    return residual_identifiers(patient, text)


def test_every_known_identifier_is_replaced_and_restored_exactly() -> None:
    r = Redactor(PATIENT)
    redacted = r.apply(TEXT)
    assert leaks(PATIENT, redacted) == 0, redacted
    assert "555-01" not in redacted
    for kept in ("Pain 4/10 since 03/02/2025", "Rosalind Zielinskiova", "callback"):  # service dates, other words
        assert kept in redacted
    assert redacted.startswith("Member: [PATIENT] | Member ID: [MEMBER_ID] | DOB: [DOB]\n")
    assert "[PATIENT_2]" in redacted and "[DOB_4]" in redacted and "[PHONE_2]" in redacted  # one per spelling
    assert r.restore(redacted) == TEXT
    assert set(r.replaced) == {"PATIENT", "MRN", "MEMBER_ID", "DOB", "PHONE"}


def test_the_four_date_spellings() -> None:
    assert date_forms(date(1959, 1, 4)) == ("01/04/1959", "1959-01-04", "January 4, 1959", "01-04-1959")


def test_a_placeholder_the_redactor_did_not_issue_is_left_alone() -> None:
    r = Redactor(PATIENT)
    r.apply("Rosa Zielinski")
    assert r.restore("[PATIENT] and [MRN_7]") == "Rosa Zielinski and [MRN_7]"


@pytest.mark.parametrize("split", SPLITS)
def test_model_view_of_every_case_holds_no_identifier_and_is_reversible(split: str) -> None:
    for case, _ in SPLITS[split]:
        view = model_view(case)
        sent = view.case.model_dump_json()
        assert leaks(case.patient, sent) == 0, case.case_id
        for page, original in zip(view.case.pages, view.page_map, strict=True):
            assert view.redactor.restore(page.text) == case.page(original).text


def _view() -> tuple[Case, ModelView]:
    pages = (
        Page(number=1, text="FAX COVER SHEET\n\nRe: Rosa Zielinski"),
        Page(number=2, text="PRIOR AUTHORIZATION REQUEST - REHABILITATION SERVICES\nMember: Rosa Zielinski"),
        Page(number=3, text="Insurance Eligibility Verification\nMember ID: EHP-SYN-85756847"),
        Page(number=4, text="Physical Therapy Re-evaluation\nRosa Zielinski reports knee pain 2/10 at rest."),
    )
    case = Case(
        case_id="c", procedure_code="PT_EXTENSION", submitted_on=date(2025, 5, 25), patient=PATIENT, pages=pages
    )
    return case, model_view(case)


def test_quotes_are_mapped_back_to_original_pages_and_verified_against_them() -> None:
    case, view = _view()
    assert view.page_map == (2, 4)  # fax cover and eligibility withheld
    assert view.case.pages[1].text == "Physical Therapy Re-evaluation\n[PATIENT] reports knee pain 2/10 at rest."
    from_model = CaseFindings(
        procedure_code="PT_EXTENSION",
        findings=(
            Finding(
                criterion_id="PT_EXTENSION.progress",
                status=Status.MET,
                evidence=(Evidence(page=2, quote="[PATIENT] reports knee pain 2/10 at rest"),),
                note="[PATIENT] improved",
            ),
            Finding(  # a page the view does not have: rejected, never silently remapped
                criterion_id="PT_EXTENSION.diagnosis",
                status=Status.MET,
                evidence=(Evidence(page=3, quote="Member ID: [MEMBER_ID]"),),
            ),
        ),
    )
    restored = view.restore(from_model)
    progress, diagnosis = restored.findings
    assert progress.evidence == (Evidence(page=4, quote="Rosa Zielinski reports knee pain 2/10 at rest"),)
    assert progress.note == "Rosa Zielinski improved"
    assert diagnosis.evidence[0].page > len(case.pages)
    verified, report = verify(case, restored)
    assert verified.findings[0].verified and verified.findings[0].status == Status.MET
    assert [i.reason for i in report.invalid] == ["page_out_of_range"]
    assert verified.findings[1].status == Status.INSUFFICIENT


def test_no_identifier_and_no_withheld_page_reaches_the_model() -> None:
    """Every request the protected system sends, for every dev case, is scanned; decisions still match gold."""
    pairs = SPLITS["dev"]
    oracle = protected_oracle(pairs)
    requests: list[dict[str, Any]] = []
    send = oracle.send

    async def recording(request: dict[str, Any]) -> dict[str, Any]:
        requests.append(request)
        return await send(request)

    oracle.send = recording  # type: ignore[method-assign]
    system = ProtectedSystem(AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(oracle)))

    async def scenario() -> None:
        for case, gold in pairs:
            requests.clear()
            run = await run_case(system, case)
            assert run.error is None and run.decision.outcome == gold.expected_decision, case.case_id
            assert run.verification.invalid == ()  # every restored quote verified on the original page
            sent = json.dumps(requests)
            assert requests and leaks(case.patient, sent) == 0, case.case_id
            withheld = [p for p in case.pages if p.number not in model_view(case).page_map]
            for page in withheld:
                assert Redactor(case.patient).apply(page.text) not in sent

    asyncio.run(scenario())


def test_the_unprotected_oracle_would_have_leaked() -> None:
    """Control for the test above: without the model view, the same scan finds the identifiers."""
    case, _ = SPLITS["dev"][0]
    oracle = OracleTransport(SPLITS["dev"][:1])
    seen: list[dict[str, Any]] = []
    send = oracle.send

    async def recording(request: dict[str, Any]) -> dict[str, Any]:
        seen.append(request)
        return await send(request)

    oracle.send = recording  # type: ignore[method-assign]
    asyncio.run(run_case(AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(oracle)), case))
    assert leaks(case.patient, json.dumps(seen)) > 0


def test_the_production_system_is_protected() -> None:
    assert isinstance(default_system(), ProtectedSystem)
