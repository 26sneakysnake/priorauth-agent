"""FHIR mapping: an inbound prior-authorization Bundle to a Case, and a decision to a simplified ClaimResponse.

FHIR version: R4B (4.3.0), validated with the R4B models of `fhir.resources` (8.x). The plain R4 (4.0.1) models only
exist in the pydantic-v1 releases of that library, so they are not used; the Bundle uses only elements whose shape is
the same in R4 and R4B. Nothing here is certified, and the ClaimResponse is only *inspired by* the Da Vinci Prior
Authorization Support (PAS) guide: it does not claim PAS conformance.

Inbound Bundle (type "collection", tagged synthetic in meta.tag):
    Patient            name, gender, birthDate, identifiers (MRN, member id)
    ServiceRequest or MedicationRequest    the requested service, coded in PROCEDURE_SYSTEM; authoredOn = request date
    Condition*         diagnosis codes (stored, informational, never used to decide)
    DocumentReference+ the faxed record: each content.attachment is one page, text/plain, base64 data, in page order
"""

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.claimresponse import ClaimResponse
from pydantic import ValidationError

from priorauth.data.schema import Case, Page, Patient
from priorauth.policy.criteria import PAYER_NAME, PROCEDURES
from priorauth.policy.engine import Decision, Outcome

BASE = "https://priorauth-agent.example/fhir"  # .example is reserved (RFC 2606): no real system is named
PROCEDURE_SYSTEM = f"{BASE}/CodeSystem/procedure"
MRN_SYSTEM = f"{BASE}/sid/mrn"
MEMBER_SYSTEM = f"{BASE}/sid/member-id"
SUBMITTER_SYSTEM = f"{BASE}/sid/submitter-case"
CASE_SYSTEM = f"{BASE}/sid/case"
TAG_SYSTEM = f"{BASE}/CodeSystem/data-origin"
SYNTHETIC_TAG = {"system": TAG_SYSTEM, "code": "synthetic"}
REVIEW_ACTION_URL = f"{BASE}/StructureDefinition/review-action"
X12_REVIEW_ACTION = "https://codesystem.x12.org/005010/306"  # X12 278 action codes, as used by Da Vinci PAS
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"

MAX_PAGES = 50
MAX_PAGE_CHARS = 100_000


class BundleError(ValueError):
    """Every problem found in a Bundle; `errors` is shaped like FastAPI's 422 details."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} problem(s) in the Bundle")
        self.errors = errors


@dataclass
class ParsedBundle:
    case: Case
    submitter_case_id: str | None
    conditions: list[dict[str, str]] = field(default_factory=list)


def _problem(loc: list[str | int], msg: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": "fhir_semantic"}


def _resources(bundle: dict[str, Any], kind: str) -> list[tuple[int, dict[str, Any]]]:
    return [
        (i, e["resource"])
        for i, e in enumerate(bundle.get("entry") or [])
        if isinstance(e.get("resource"), dict) and e["resource"].get("resourceType") == kind
    ]


def _coding(concept: dict[str, Any] | None, system: str) -> dict[str, Any] | None:
    for c in (concept or {}).get("coding") or []:
        if c.get("system") == system:
            return dict(c)
    return None


def _patient(index: int, res: dict[str, Any], problems: list[dict[str, Any]]) -> Patient | None:
    loc: list[str | int] = ["entry", index, "resource"]
    ids = {i.get("system"): i.get("value") for i in res.get("identifier") or []}
    names = res.get("name") or []
    name = ""
    if names:
        n = names[0]
        name = n.get("text") or " ".join([*(n.get("given") or []), n.get("family") or ""]).strip()
    checks = [
        (bool(name), "name", "Patient.name is required"),
        (res.get("gender") in ("female", "male"), "gender", "Patient.gender must be 'female' or 'male'"),
        (bool(res.get("birthDate")), "birthDate", "Patient.birthDate is required"),
        (bool(ids.get(MRN_SYSTEM)), "identifier", f"Patient.identifier with system {MRN_SYSTEM} is required"),
        (bool(ids.get(MEMBER_SYSTEM)), "identifier", f"Patient.identifier with system {MEMBER_SYSTEM} is required"),
    ]
    bad = [(where, msg) for ok, where, msg in checks if not ok]
    problems.extend(_problem([*loc, where], msg) for where, msg in bad)
    if bad:
        return None
    return Patient(
        name=name,
        birth_date=date.fromisoformat(res["birthDate"]),
        sex=res["gender"],
        mrn=ids[MRN_SYSTEM],
        member_id=ids[MEMBER_SYSTEM],
    )


def _request(bundle: dict[str, Any], problems: list[dict[str, Any]]) -> tuple[str, date] | None:
    candidates = []
    for kind, code_field in (("ServiceRequest", "code"), ("MedicationRequest", "medicationCodeableConcept")):
        for i, res in _resources(bundle, kind):
            coding = _coding(res.get(code_field), PROCEDURE_SYSTEM)
            if coding is not None:
                candidates.append((i, kind, res, coding))
    if len(candidates) != 1:
        problems.append(
            _problem(
                ["entry"],
                "exactly one ServiceRequest or MedicationRequest coded in "
                f"{PROCEDURE_SYSTEM} is required, found {len(candidates)}",
            )
        )
        return None
    i, kind, res, coding = candidates[0]
    loc: list[str | int] = ["entry", i, "resource"]
    code = coding.get("code")
    if code not in PROCEDURES:
        problems.append(_problem([*loc, "code"], f"unknown procedure code {code!r}; known: {sorted(PROCEDURES)}"))
        return None
    authored = res.get("authoredOn")
    if not authored:
        problems.append(_problem([*loc, "authoredOn"], f"{kind}.authoredOn (date of the request) is required"))
        return None
    return code, date.fromisoformat(str(authored)[:10])


def _pages(bundle: dict[str, Any], problems: list[dict[str, Any]]) -> list[Page]:
    pages: list[Page] = []
    docs = _resources(bundle, "DocumentReference")
    if not docs:
        problems.append(_problem(["entry"], "at least one DocumentReference with the record pages is required"))
    for i, res in docs:
        for j, content in enumerate(res.get("content") or []):
            loc: list[str | int] = ["entry", i, "resource", "content", j, "attachment"]
            att = content.get("attachment") or {}
            if not str(att.get("contentType", "")).startswith("text/plain"):
                problems.append(_problem([*loc, "contentType"], "each page must be a text/plain attachment"))
                continue
            try:
                text = base64.b64decode(att.get("data") or "", validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                problems.append(_problem([*loc, "data"], "attachment data must be base64-encoded UTF-8 text"))
                continue
            if not text.strip() or len(text) > MAX_PAGE_CHARS:
                problems.append(
                    _problem([*loc, "data"], f"a page must be non-empty and at most {MAX_PAGE_CHARS} chars")
                )
                continue
            pages.append(Page(number=len(pages) + 1, text=text))
    if len(pages) > MAX_PAGES:
        problems.append(_problem(["entry"], f"at most {MAX_PAGES} pages are accepted, got {len(pages)}"))
    return pages


def parse_bundle(payload: Any, case_id: str) -> ParsedBundle:
    """Validate an inbound Bundle (structure with fhir.resources, then the rules above) and build the Case."""
    if not isinstance(payload, dict) or payload.get("resourceType") != "Bundle":
        raise BundleError([_problem([], "the body must be a FHIR Bundle (resourceType 'Bundle')")])
    try:
        Bundle.model_validate(payload)
    except ValidationError as e:
        raise BundleError(
            [{"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in e.errors(include_url=False)]
        ) from None
    problems: list[dict[str, Any]] = []
    if payload.get("type") != "collection":
        problems.append(_problem(["type"], "Bundle.type must be 'collection'"))
    if SYNTHETIC_TAG not in [
        {"system": t.get("system"), "code": t.get("code")} for t in (payload.get("meta") or {}).get("tag") or []
    ]:
        problems.append(
            _problem(["meta", "tag"], f"this deployment accepts synthetic data only: tag {TAG_SYSTEM}|synthetic")
        )
    patients = _resources(payload, "Patient")
    patient = None
    if len(patients) != 1:
        problems.append(_problem(["entry"], f"exactly one Patient is required, found {len(patients)}"))
    else:
        patient = _patient(*patients[0], problems)
    request = _request(payload, problems)
    pages = _pages(payload, problems)
    conditions = []
    for _, res in _resources(payload, "Condition"):
        for c in (res.get("code") or {}).get("coding") or []:
            conditions.append({"system": str(c.get("system", "")), "code": str(c.get("code", ""))})
    if problems or patient is None or request is None:
        raise BundleError(problems)
    procedure_code, submitted_on = request
    case = Case(
        case_id=case_id, procedure_code=procedure_code, submitted_on=submitted_on, patient=patient, pages=tuple(pages)
    )
    submitter = (
        (payload.get("identifier") or {}).get("value")
        if (payload.get("identifier") or {}).get("system") == SUBMITTER_SYSTEM
        else None
    )
    return ParsedBundle(case=case, submitter_case_id=submitter, conditions=conditions)


# --------------------------------------------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------------------------------------------

_DIAGNOSIS = {
    "MRI_LUMBAR": ("M54.16", "Radiculopathy, lumbar region"),
    "PT_EXTENSION": ("M17.9", "Osteoarthritis of knee, unspecified"),
    "BIOLOGIC_RA": ("M06.9", "Rheumatoid arthritis, unspecified"),
}


def case_to_bundle(case: Case, submitter_case_id: str | None = None) -> dict[str, Any]:
    """The Bundle a submitter would send for a synthetic case (used by tests, the load test and the demo)."""
    p, procedure = case.patient, PROCEDURES[case.procedure_code]
    given, _, family = p.name.rpartition(" ")
    coding = {"system": PROCEDURE_SYSTEM, "code": procedure.code, "display": procedure.name}
    subject = {"reference": "urn:uuid:patient"}
    if case.procedure_code == "BIOLOGIC_RA":
        request = {
            "resourceType": "MedicationRequest",
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {"coding": [coding]},
        }
    else:
        request = {
            "resourceType": "ServiceRequest",
            "status": "active",
            "intent": "order",
            "code": {"coding": [coding]},
        }
    request |= {"subject": subject, "authoredOn": case.submitted_on.isoformat()}
    dx_code, dx_display = _DIAGNOSIS[case.procedure_code]
    entries: list[dict[str, Any]] = [
        {
            "fullUrl": "urn:uuid:patient",
            "resource": {
                "resourceType": "Patient",
                "identifier": [{"system": MRN_SYSTEM, "value": p.mrn}, {"system": MEMBER_SYSTEM, "value": p.member_id}],
                "name": [{"text": p.name, "family": family, "given": [given]}],
                "gender": p.sex,
                "birthDate": p.birth_date.isoformat(),
            },
        },
        {"fullUrl": "urn:uuid:request", "resource": request},
        {
            "fullUrl": "urn:uuid:condition",
            "resource": {
                "resourceType": "Condition",
                "subject": subject,
                "code": {"coding": [{"system": ICD10CM, "code": dx_code, "display": dx_display}]},
            },
        },
        {
            "fullUrl": "urn:uuid:record",
            "resource": {
                "resourceType": "DocumentReference",
                "status": "current",
                "subject": subject,
                "content": [
                    {
                        "attachment": {
                            "contentType": "text/plain; charset=utf-8",
                            "data": base64.b64encode(page.text.encode()).decode(),
                            "title": f"Page {page.number}",
                        }
                    }
                    for page in case.pages
                ],
            },
        },
    ]
    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "meta": {"tag": [SYNTHETIC_TAG]},
        "type": "collection",
        "timestamp": f"{case.submitted_on.isoformat()}T12:00:00Z",
        "entry": entries,
    }
    if submitter_case_id:
        bundle["identifier"] = {"system": SUBMITTER_SYSTEM, "value": submitter_case_id}
    return bundle


_ACTIONS = {
    Outcome.APPROVE: ("A1", "Certified in total", "complete"),
    Outcome.PEND: ("A4", "Pended", "queued"),  # queued: waiting for a human reviewer
}


def claim_response(case_id: str, member_id: str, decision: Decision, created: datetime) -> dict[str, Any]:
    """Simplified ClaimResponse inspired by Da Vinci PAS. There is no denial: PEND means human review."""
    code, display, outcome = _ACTIONS[decision.outcome]
    notes = [
        {"number": n, "type": "display", "text": f"{r.code.value}" + (f" {r.criterion_id}" if r.criterion_id else "")}
        for n, r in enumerate(decision.reasons, start=1)
    ]
    body: dict[str, Any] = {
        "resourceType": "ClaimResponse",
        "id": case_id,
        "meta": {"tag": [SYNTHETIC_TAG]},
        "identifier": [{"system": CASE_SYSTEM, "value": case_id}],
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "preauthorization",
        "patient": {"identifier": {"system": MEMBER_SYSTEM, "value": member_id}},
        "created": (created if created.tzinfo else created.replace(tzinfo=UTC)).replace(microsecond=0).isoformat(),
        "insurer": {"display": PAYER_NAME},
        "outcome": outcome,
        "disposition": "Approved: every criterion met with verified evidence"
        if decision.outcome is Outcome.APPROVE
        else f"Pended for clinical review ({len(decision.reasons)} reason(s))",
        "extension": [
            {
                "url": REVIEW_ACTION_URL,
                "valueCodeableConcept": {"coding": [{"system": X12_REVIEW_ACTION, "code": code, "display": display}]},
            }
        ],
    }
    if decision.outcome is Outcome.APPROVE:
        body["preAuthRef"] = case_id
    if notes:
        body["processNote"] = notes
    ClaimResponse.model_validate(body)  # never return something the R4B model rejects
    return body
