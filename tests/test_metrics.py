"""Metrics on hand-drawn cases whose expected values are computed by hand in the comments."""

import pytest

from eval.metrics import compute, percentile, rate, wilson
from priorauth.agent.verifier import VerificationReport
from priorauth.data.schema import Gold, GoldCriterion, Trap
from priorauth.pipeline import CaseRun, Usage
from priorauth.policy.engine import Decision, Outcome
from priorauth.policy.models import CaseFindings, Status

M, N, INS = Status.MET, Status.NOT_MET, Status.INSUFFICIENT


def pair(
    cid: str,
    expected: str,
    predicted: Outcome,
    gold_statuses: tuple[Status, Status],
    pred_statuses: tuple[Status, Status],
    *,
    quotes: tuple[int, int],
    cost: float,
    latency: float | None,
    injection: bool = False,
    flagged: bool = False,
    trap: Trap = Trap.CLEAN_APPROVE,
) -> tuple[Gold, CaseRun]:
    gold = Gold(
        case_id=cid,
        procedure_code="P",
        trap=trap,
        target_criterion=None,
        expected_decision=expected,  # type: ignore[arg-type]
        criteria={
            k: GoldCriterion(status=s, evidence=(), rationale="")
            for k, s in zip(("P.a", "P.b"), gold_statuses, strict=True)
        },
        injection=injection,
        injection_evidence=(),
        paraphrased=(),
        page_kinds=(),
    )
    findings = CaseFindings(procedure_code="P", findings=(), injection_detected=flagged)
    run = CaseRun(
        case_id=cid,
        raw=findings,
        verified=findings,
        verification=VerificationReport(n_quotes=quotes[0], n_valid=quotes[1], invalid=(), downgraded=()),
        decision=Decision(
            outcome=predicted, reasons=(), criteria=dict(zip(("P.a", "P.b"), pred_statuses, strict=True))
        ),
        usage=Usage(cost_usd=cost, latency_ms=latency),
    )
    return gold, run


A, P = Outcome.APPROVE, Outcome.PEND
CASES = [
    # id    expected   pred  gold     retained  quotes  cost  latency
    pair("c1", "APPROVE", A, (M, M), (M, M), quotes=(2, 2), cost=0.00, latency=100.0),
    pair("c2", "APPROVE", P, (M, M), (M, INS), quotes=(2, 1), cost=0.01, latency=200.0),  # unjustified PEND
    pair("c3", "PEND", A, (N, M), (M, M), quotes=(2, 2), cost=0.02, latency=None, trap=Trap.CLEAN_PEND),  # dangerous
    pair("c4", "PEND", P, (INS, M), (INS, M), quotes=(1, 1), cost=0.03, latency=300.0, injection=True, flagged=True,
         trap=Trap.INJECTION),
    pair("c5", "PEND", P, (N, M), (N, M), quotes=(3, 2), cost=0.04, latency=400.0, flagged=True, trap=Trap.CLEAN_PEND),
]  # fmt: skip


def test_wilson_matches_reference_values() -> None:
    assert wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert wilson(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)
    assert wilson(1, 3) == pytest.approx((0.0615, 0.7923), abs=1e-4)
    with pytest.raises(ValueError):
        wilson(1, 0)


def test_rate_of_empty_population_has_no_value() -> None:
    r = rate(0, 0)
    assert r.value is None and r.ci95 is None


def test_percentile_linear_interpolation() -> None:
    assert percentile([0.0, 0.01, 0.02, 0.03, 0.04], 0.5) == pytest.approx(0.02)
    assert percentile([0.0, 0.01, 0.02, 0.03, 0.04], 0.95) == pytest.approx(0.038)  # rank 3.8
    assert percentile([5.0], 0.95) == 5.0


def test_decision_metrics() -> None:
    m = compute(CASES)
    assert (m.n_cases, m.expected_pend, m.expected_approve) == (5, 3, 2)
    assert (m.dangerous_approval_rate.k, m.dangerous_approval_rate.n) == (1, 3)  # c3
    assert m.dangerous_approval_rate.ci95 == pytest.approx((0.0615, 0.7923), abs=1e-4)
    assert (m.unjustified_pend_rate.k, m.unjustified_pend_rate.n) == (1, 2)  # c2
    assert (m.decision_accuracy.k, m.decision_accuracy.n) == (3, 5)


def test_criterion_metrics() -> None:
    m = compute(CASES)
    assert (m.criterion_accuracy.k, m.criterion_accuracy.n) == (8, 10)  # misses: c2.b, c3.a
    assert {c: (r.k, r.n) for c, r in m.criterion_accuracy_by_id.items()} == {"P.a": (4, 5), "P.b": (4, 5)}
    assert m.criterion_confusion == {
        "MET": {"MET": 6, "NOT_MET": 0, "INSUFFICIENT": 1},
        "NOT_MET": {"MET": 1, "NOT_MET": 1, "INSUFFICIENT": 0},
        "INSUFFICIENT": {"MET": 0, "NOT_MET": 0, "INSUFFICIENT": 1},
    }


def test_evidence_injection_and_usage_metrics() -> None:
    m = compute(CASES)
    assert (m.evidence_validity.k, m.evidence_validity.n) == (8, 10)
    assert (m.injection_detection.k, m.injection_detection.n) == (1, 1)
    assert (m.false_injection_flags.k, m.false_injection_flags.n) == (1, 4)  # c5
    assert m.usage.total_cost_usd == pytest.approx(0.10)
    assert m.usage.cost_usd_per_case.p50 == pytest.approx(0.02)
    assert m.usage.cost_usd_per_case.p95 == pytest.approx(0.038)
    lat = m.usage.latency_ms_per_case
    assert (lat.n, lat.p50) == (4, 250.0)  # c3 has no latency
    assert lat.p95 == pytest.approx(385.0)


def test_breakdowns() -> None:
    m = compute(CASES)
    assert m.by_trap["clean_pend"].n_cases == 2
    assert m.by_trap["clean_pend"].dangerous_approvals == 1
    assert m.by_trap["clean_approve"].unjustified_pends == 1
    assert m.by_procedure["P"].decision_accuracy.k == 3


def test_mismatched_pairs_are_rejected() -> None:
    gold, _ = CASES[0]
    _, run = CASES[1]
    with pytest.raises(ValueError):
        compute([(gold, run)])
