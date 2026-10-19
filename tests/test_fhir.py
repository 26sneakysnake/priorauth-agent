"""FHIR mapping: every synthetic case survives a Bundle round trip; bad Bundles get explicit errors."""

import base64
import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.claimresponse import ClaimResponse

from priorauth.api import fhir
from priorauth.policy.engine import decide
from priorauth.policy.models import CaseFindings
from tests.test_data import load
from tests.test_robust import cases as robust_cases

ALL = load("dev") + load("test") + robust_cases()
CASE = next(c for c, _ in ALL if c.procedure_code != "BIOLOGIC_RA")  # a ServiceRequest case, for the edits below


def bundle() -> dict[str, Any]:
    copy_: dict[str, Any] = json.loads(json.dumps(fhir.case_to_bundle(CASE, "SUB-1")))
    return copy_


def errors_of(payload: Any) -> list[dict[str, Any]]:
    with pytest.raises(fhir.BundleError) as e:
        fhir.parse_bundle(payload, "case-x")
    return e.value.errors


def test_every_case_round_trips_through_a_valid_r4b_bundle() -> None:
    for case, _ in ALL:
        payload = json.loads(json.dumps(fhir.case_to_bundle(case, case.case_id)))
        Bundle.model_validate(payload)
        parsed = fhir.parse_bundle(payload, case.case_id)
        assert parsed.case == case
        assert parsed.submitter_case_id == case.case_id
        assert parsed.conditions and parsed.conditions[0]["system"] == fhir.ICD10CM


def test_structural_errors_come_from_the_fhir_models() -> None:
    b = bundle()
    b["entry"][0]["resource"]["birthDate"] = "31/12/1960"
    [err] = errors_of(b)
    assert err["loc"][:4] == ["entry", 0, "resource", "birthDate"]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda b: b.update(type="transaction"), "Bundle.type must be 'collection'"),
        (lambda b: b["meta"].update(tag=[]), "synthetic data only"),
        (lambda b: b["entry"].pop(0), "exactly one Patient is required, found 0"),
        (lambda b: b["entry"].pop(1), "exactly one ServiceRequest or MedicationRequest"),
        (lambda b: b["entry"].pop(3), "at least one DocumentReference"),
        (lambda b: b["entry"][0]["resource"].pop("identifier"), "member-id is required"),
        (lambda b: b["entry"][1]["resource"]["code"]["coding"][0].update(code="KNEE_SURGERY"), "unknown procedure"),
        (lambda b: b["entry"][1]["resource"].pop("authoredOn"), "authoredOn"),
        (
            lambda b: b["entry"][3]["resource"]["content"][0]["attachment"].update(contentType="application/pdf"),
            "text/plain",
        ),
        (lambda b: b["entry"][3]["resource"]["content"][0]["attachment"].update(data="%%%"), "base64"),
        (
            lambda b: b["entry"][3]["resource"]["content"][0]["attachment"].update(
                data=base64.b64encode(b"\xff\xfe").decode()
            ),
            "UTF-8",
        ),
    ],
)
def test_semantic_errors_are_explicit(edit: Any, message: str) -> None:
    b = bundle()
    edit(b)
    errs = errors_of(b)
    assert any(message in e["msg"] for e in errs), errs


def test_not_a_bundle() -> None:
    assert "FHIR Bundle" in errors_of({"resourceType": "Patient"})[0]["msg"]
    assert "FHIR Bundle" in errors_of([1, 2])[0]["msg"]


def test_several_problems_are_reported_together() -> None:
    b = copy.deepcopy(bundle())
    b["type"] = "batch"
    b["entry"].pop(0)
    assert len(errors_of(b)) >= 2


@pytest.mark.parametrize("index", [0, 5])
def test_claim_response_is_a_valid_r4b_resource_and_never_denies(index: int) -> None:
    case, _ = ALL[index]
    decision = decide(CaseFindings(procedure_code=case.procedure_code, findings=()))
    body = fhir.claim_response(case.case_id, case.patient.member_id, decision, datetime.now(UTC))
    ClaimResponse.model_validate(body)
    assert body["use"] == "preauthorization" and body["outcome"] == "queued"
    [action] = body["extension"]
    assert action["valueCodeableConcept"]["coding"][0]["code"] == "A4"  # pended, never A3 (not certified)
    assert case.patient.name not in json.dumps(body)


def test_claim_response_after_a_reviewer_approval_keeps_the_engines_pend() -> None:
    case, _ = ALL[0]
    decision = decide(CaseFindings(procedure_code=case.procedure_code, findings=()))
    reviewer = fhir.ReviewerDecision(review_id="r-1", reviewer_id="u-1", decided_at=datetime(2026, 1, 2, 3, 4, 5))
    body = fhir.claim_response(case.case_id, case.patient.member_id, decision, datetime.now(UTC), reviewer)
    ClaimResponse.model_validate(body)
    assert body["outcome"] == "complete" and body["preAuthRef"] == case.case_id
    assert body["extension"][0]["valueCodeableConcept"]["coding"][0]["code"] == "A1"
    assert body["created"] == "2026-01-02T03:04:05+00:00"
    notes = [n["text"] for n in body["processNote"]]
    assert len(notes) == len(decision.reasons) + 1
    assert all(t.startswith("engine PEND: ") for t in notes[:-1])
    assert notes[-1] == "reviewer APPROVE: review r-1 by user u-1"
