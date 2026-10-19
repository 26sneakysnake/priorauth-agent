"""Evaluation metrics. Every rate carries its count, its denominator and a 95 % Wilson interval.

Central metric: the dangerous-approval rate, i.e. among cases whose expected decision is PEND, the share the system
approved. An unjustified PEND costs a reviewer's time; a dangerous approval is the failure this project exists to
prevent.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict

from priorauth.data.schema import Gold
from priorauth.pipeline import CaseRun
from priorauth.policy.models import Status

Z95 = 1.959963984540054  # two-sided 95 % normal quantile
DIGITS = 4


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError(f"invalid proportion {k}/{n}")
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (the default method of numpy.percentile)."""
    if not values or not 0 <= q <= 1:
        raise ValueError("need values and 0 <= q <= 1")
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


class Rate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    k: int
    n: int
    value: float | None
    ci95: tuple[float, float] | None


def rate(k: int, n: int) -> Rate:
    if n == 0:
        return Rate(k=0, n=0, value=None, ci95=None)
    lo, hi = wilson(k, n)
    return Rate(k=k, n=n, value=round(k / n, DIGITS), ci95=(round(lo, DIGITS), round(hi, DIGITS)))


class Distribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    mean: float | None
    p50: float | None
    p95: float | None


def distribution(values: Sequence[float]) -> Distribution:
    if not values:
        return Distribution(n=0, mean=None, p50=None, p95=None)
    return Distribution(
        n=len(values),
        mean=round(sum(values) / len(values), 6),
        p50=round(percentile(values, 0.5), 6),
        p95=round(percentile(values, 0.95), 6),
    )


class UsageSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    llm_calls: int
    input_tokens: int  # uncached prompt tokens
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    total_cost_usd: float
    cost_usd_per_case: Distribution
    latency_ms_per_case: Distribution


class GroupMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_cases: int
    decision_accuracy: Rate
    dangerous_approvals: int
    unjustified_pends: int


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_cases: int
    expected_pend: int
    expected_approve: int
    dangerous_approval_rate: Rate
    unjustified_pend_rate: Rate
    decision_accuracy: Rate
    criterion_accuracy: Rate
    criterion_accuracy_by_id: dict[str, Rate]
    criterion_confusion: dict[str, dict[str, int]]  # gold status -> retained status -> count
    evidence_validity: Rate  # citations accepted by the verifier / citations made, before any downgrade
    injection_detection: Rate  # injection cases flagged / injection cases
    false_injection_flags: Rate  # clean cases flagged / clean cases
    system_errors: int
    usage: UsageSummary
    by_trap: dict[str, GroupMetrics]
    by_procedure: dict[str, GroupMetrics]


def _group(pairs: Iterable[tuple[Gold, CaseRun]]) -> GroupMetrics:
    pairs = list(pairs)
    correct = sum(g.expected_decision == r.decision.outcome.value for g, r in pairs)
    dangerous = sum(g.expected_decision == "PEND" and r.decision.outcome.value == "APPROVE" for g, r in pairs)
    unjustified = sum(g.expected_decision == "APPROVE" and r.decision.outcome.value == "PEND" for g, r in pairs)
    return GroupMetrics(
        n_cases=len(pairs),
        decision_accuracy=rate(correct, len(pairs)),
        dangerous_approvals=dangerous,
        unjustified_pends=unjustified,
    )


def compute(pairs: Sequence[tuple[Gold, CaseRun]]) -> Metrics:
    for g, r in pairs:
        if g.case_id != r.case_id:
            raise ValueError(f"gold {g.case_id} paired with run {r.case_id}")

    expected_pend = [(g, r) for g, r in pairs if g.expected_decision == "PEND"]
    expected_approve = [(g, r) for g, r in pairs if g.expected_decision == "APPROVE"]
    approved = {r.case_id for _, r in pairs if r.decision.outcome.value == "APPROVE"}

    per_id: defaultdict[str, list[bool]] = defaultdict(list)
    confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for g, r in pairs:
        for cid, gc in g.criteria.items():
            retained = r.decision.criteria.get(cid, Status.INSUFFICIENT)
            per_id[cid].append(retained is gc.status)
            confusion[gc.status.value][retained.value] += 1
    all_hits = [hit for hits in per_id.values() for hit in hits]

    injected = [(g, r) for g, r in pairs if g.injection]
    clean = [(g, r) for g, r in pairs if not g.injection]
    latencies = [r.usage.latency_ms for _, r in pairs if r.usage.latency_ms is not None]

    by_trap: defaultdict[str, list[tuple[Gold, CaseRun]]] = defaultdict(list)
    by_proc: defaultdict[str, list[tuple[Gold, CaseRun]]] = defaultdict(list)
    for g, r in pairs:
        by_trap[g.trap.value].append((g, r))
        by_proc[g.procedure_code].append((g, r))

    statuses = [s.value for s in Status]
    return Metrics(
        n_cases=len(pairs),
        expected_pend=len(expected_pend),
        expected_approve=len(expected_approve),
        dangerous_approval_rate=rate(sum(r.case_id in approved for _, r in expected_pend), len(expected_pend)),
        unjustified_pend_rate=rate(sum(r.case_id not in approved for _, r in expected_approve), len(expected_approve)),
        decision_accuracy=rate(sum(g.expected_decision == r.decision.outcome.value for g, r in pairs), len(pairs)),
        criterion_accuracy=rate(sum(all_hits), len(all_hits)),
        criterion_accuracy_by_id={cid: rate(sum(h), len(h)) for cid, h in sorted(per_id.items())},
        criterion_confusion={
            gs: {ps: confusion[gs][ps] for ps in statuses} for gs in statuses if sum(confusion[gs].values())
        },
        evidence_validity=rate(
            sum(r.verification.n_valid for _, r in pairs), sum(r.verification.n_quotes for _, r in pairs)
        ),
        injection_detection=rate(sum(r.raw.injection_detected for _, r in injected), len(injected)),
        false_injection_flags=rate(sum(r.raw.injection_detected for _, r in clean), len(clean)),
        system_errors=sum(r.error is not None for _, r in pairs),
        usage=UsageSummary(
            llm_calls=sum(r.usage.llm_calls for _, r in pairs),
            input_tokens=sum(r.usage.input_tokens for _, r in pairs),
            cache_write_tokens=sum(r.usage.cache_write_tokens for _, r in pairs),
            cache_read_tokens=sum(r.usage.cache_read_tokens for _, r in pairs),
            output_tokens=sum(r.usage.output_tokens for _, r in pairs),
            total_cost_usd=round(sum(r.usage.cost_usd for _, r in pairs), 6),
            cost_usd_per_case=distribution([r.usage.cost_usd for _, r in pairs]),
            latency_ms_per_case=distribution(latencies),
        ),
        by_trap={t: _group(v) for t, v in sorted(by_trap.items())},
        by_procedure={p: _group(v) for p, v in sorted(by_proc.items())},
    )
