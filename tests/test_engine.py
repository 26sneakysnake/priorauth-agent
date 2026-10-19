"""The policy engine approves only fully met, verified, conflict-free, injection-free cases; it never denies."""

import itertools
import re
from functools import cache
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from priorauth.data.schema import Gold
from priorauth.policy.criteria import PROCEDURES, Procedure
from priorauth.policy.engine import Outcome, ReasonCode, decide
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status

ROOT = Path(__file__).resolve().parents[1]
EV = (Evidence(page=1, quote="a quote long enough"),)


@cache
def kind(name: str, cid: str) -> tuple[Finding, ...]:
    return {
        "met": (Finding(criterion_id=cid, status=Status.MET, evidence=EV, verified=True),),
        "met_unverified": (Finding(criterion_id=cid, status=Status.MET, evidence=EV, verified=False),),
        "met_no_evidence": (Finding(criterion_id=cid, status=Status.MET, verified=True),),
        "met_conflict": (Finding(criterion_id=cid, status=Status.MET, evidence=EV, verified=True, conflict=True),),
        "not_met": (Finding(criterion_id=cid, status=Status.NOT_MET, evidence=EV, verified=True),),
        "insufficient": (Finding(criterion_id=cid, status=Status.INSUFFICIENT),),
        "missing": (),
        "duplicate_disagrees": (
            Finding(criterion_id=cid, status=Status.MET, evidence=EV, verified=True),
            Finding(criterion_id=cid, status=Status.NOT_MET, evidence=EV, verified=True),
        ),
    }[name]


# Every way one criterion can be reported, and the reason code it must produce (None: usable MET).
EXPECTED_REASON = {
    "met": None,
    "met_unverified": ReasonCode.UNVERIFIED_EVIDENCE,
    "met_no_evidence": ReasonCode.NO_EVIDENCE,
    "met_conflict": ReasonCode.CONFLICT,
    "not_met": ReasonCode.NOT_MET,
    "insufficient": ReasonCode.INSUFFICIENT,
    "missing": ReasonCode.MISSING_FINDING,
    "duplicate_disagrees": ReasonCode.DUPLICATE_DISAGREES,
}


def test_outcome_has_exactly_two_values() -> None:
    assert {o.value for o in Outcome} == {"APPROVE", "PEND"}


def test_no_deny_outcome_anywhere_in_the_source() -> None:
    pattern = re.compile(r"\bDEN(Y|IED|IAL)\b")
    offenders = [p for p in (ROOT / "src").rglob("*.py") if pattern.search(p.read_text())]
    assert offenders == []


@pytest.mark.parametrize("procedure", PROCEDURES.values(), ids=lambda p: p.code)
def test_exhaustive_combinations_of_criterion_reports(procedure: Procedure) -> None:
    """All 8^n combinations of per-criterion reports, with and without a detected injection."""
    ids = procedure.criterion_ids
    for combo in itertools.product(EXPECTED_REASON, repeat=len(ids)):
        findings = tuple(f for name, cid in zip(combo, ids, strict=True) for f in kind(name, cid))
        for injection in (False, True):
            d = decide(CaseFindings(procedure_code=procedure.code, findings=findings, injection_detected=injection))
            should_approve = all(name == "met" for name in combo) and not injection
            assert d.outcome is (Outcome.APPROVE if should_approve else Outcome.PEND)
            assert bool(d.reasons) == (d.outcome is Outcome.PEND)
            codes = {(r.code, r.criterion_id) for r in d.reasons}
            for name, cid in zip(combo, ids, strict=True):
                expected = EXPECTED_REASON[name]
                if expected is not None:
                    assert (expected, cid) in codes
                assert (d.criteria[cid] is Status.MET) == (name == "met")
            assert ((ReasonCode.INJECTION_DETECTED, None) in codes) == injection


def test_unknown_procedure_pends() -> None:
    d = decide(CaseFindings(procedure_code="CT_HEAD", findings=()))
    assert d.outcome is Outcome.PEND
    assert d.reasons[0].code is ReasonCode.UNKNOWN_PROCEDURE


def test_finding_for_a_criterion_outside_the_policy_pends() -> None:
    proc = PROCEDURES["PT_EXTENSION"]
    findings = tuple(f for cid in proc.criterion_ids for f in kind("met", cid))
    extra = Finding(criterion_id="PT_EXTENSION.vibes", status=Status.MET, evidence=EV, verified=True)
    assert decide(CaseFindings(procedure_code=proc.code, findings=findings)).outcome is Outcome.APPROVE
    d = decide(CaseFindings(procedure_code=proc.code, findings=(*findings, extra)))
    assert d.outcome is Outcome.PEND
    assert [r.code for r in d.reasons] == [ReasonCode.UNKNOWN_CRITERION]


def test_agreeing_duplicates_are_accepted() -> None:
    proc = PROCEDURES["BIOLOGIC_RA"]
    findings = tuple(f for cid in proc.criterion_ids for f in kind("met", cid) * 2)
    assert decide(CaseFindings(procedure_code=proc.code, findings=findings)).outcome is Outcome.APPROVE


def test_engine_on_gold_labels_reproduces_every_expected_decision() -> None:
    golds = sorted((ROOT / "data").glob("*/gold/*.json"))
    assert len(golds) == 200  # dev 100, test 60, robust 40
    for path in golds:
        gold = Gold.model_validate_json(path.read_bytes())
        findings = tuple(
            Finding(criterion_id=cid, status=g.status, evidence=g.evidence, verified=True)
            for cid, g in gold.criteria.items()
        )
        d = decide(
            CaseFindings(procedure_code=gold.procedure_code, findings=findings, injection_detected=gold.injection)
        )
        assert d.outcome.value == gold.expected_decision, gold.case_id
        assert d.criteria == {cid: g.status for cid, g in gold.criteria.items()}


# --- property: arbitrary, even malformed, findings never yield anything but APPROVE/PEND -------------------------

ALL_IDS = [cid for p in PROCEDURES.values() for cid in p.criterion_ids] + ["X.unknown"]
evidence = st.builds(Evidence, page=st.integers(1, 9), quote=st.text(min_size=1, max_size=20))
findings = st.builds(
    Finding,
    criterion_id=st.sampled_from(ALL_IDS),
    status=st.sampled_from(list(Status)),
    evidence=st.lists(evidence, max_size=2).map(tuple),
    conflict=st.booleans(),
    verified=st.booleans(),
)
case_findings = st.builds(
    CaseFindings,
    procedure_code=st.sampled_from([*PROCEDURES, "UNKNOWN"]),
    findings=st.lists(findings, max_size=12).map(tuple),
    injection_detected=st.booleans(),
)


@settings(max_examples=500, deadline=None)
@given(case_findings)
def test_property_outcome_is_approve_or_pend_and_approve_is_earned(cf: CaseFindings) -> None:
    d = decide(cf)
    assert d.outcome in (Outcome.APPROVE, Outcome.PEND)
    if d.outcome is Outcome.APPROVE:
        proc = PROCEDURES[cf.procedure_code]
        assert not cf.injection_detected
        assert {f.criterion_id for f in cf.findings} == set(proc.criterion_ids)
        for f in cf.findings:
            assert f.status is Status.MET and f.verified and not f.conflict
        for cid in proc.criterion_ids:
            assert any(f.evidence for f in cf.findings if f.criterion_id == cid)
