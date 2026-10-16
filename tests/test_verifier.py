from datetime import date

from priorauth.agent.verifier import MIN_QUOTE_CHARS, VerificationReport, normalize, normalize_quote, verify
from priorauth.data.schema import Case, Page, Patient
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status

CASE = Case(
    case_id="t-1",
    procedure_code="MRI_LUMBAR",
    submitted_on=date(2025, 1, 2),
    patient=Patient(
        name="Test Person", birth_date=date(1980, 1, 1), sex="female", mrn="SYN-0000001", member_id="EHP-SYN-1"
    ),
    pages=(
        Page(number=1, text="Clinical indication: Left L5 radiculopathy.\nUrgency: Routine\n"),
        Page(number=2, text="HPI:\nLow back pain radiating to the left\nleg for 9 weeks. Denies fever.\n"),
    ),
)


def finding(status: Status, *evidence: Evidence, cid: str = "MRI_LUMBAR.symptom_duration") -> Finding:
    # verified=True on input must not survive: only the verifier decides.
    return Finding(criterion_id=cid, status=status, evidence=evidence, verified=True)


def run(*findings: Finding, injection: tuple[Evidence, ...] = ()) -> tuple[CaseFindings, VerificationReport]:
    return verify(CASE, CaseFindings(procedure_code="MRI_LUMBAR", findings=findings, injection_evidence=injection))


def test_normalization_ignores_case_whitespace_and_typography() -> None:
    assert normalize("Left  L5\n‘radiculopathy’ – x") == "left l5 'radiculopathy' - x"  # noqa: RUF001 - typographic characters are the point of this test
    assert normalize_quote('  "...leg for 9 weeks..."  ') == "leg for 9 weeks"
    assert normalize_quote("…leg for 9 weeks.") == "leg for 9 weeks"


def test_exact_and_reflowed_quotes_are_accepted() -> None:
    out, report = run(
        finding(Status.MET, Evidence(page=2, quote="low back pain radiating to the left leg for 9 weeks")),
    )
    assert out.findings[0].verified and out.findings[0].status is Status.MET
    assert report.n_quotes == report.n_valid == 1


def test_quote_on_the_wrong_page_is_rejected_and_met_is_downgraded() -> None:
    out, report = run(finding(Status.MET, Evidence(page=1, quote="leg for 9 weeks")))
    f = out.findings[0]
    assert not f.verified and f.status is Status.INSUFFICIENT and "not verified" in f.note
    assert [i.reason for i in report.invalid] == ["not_found"]
    assert report.downgraded == ("MRI_LUMBAR.symptom_duration",)


def test_invented_page_too_short_quote_and_missing_evidence() -> None:
    out, report = run(
        finding(Status.MET, Evidence(page=7, quote="leg for 9 weeks")),
        finding(Status.NOT_MET, Evidence(page=1, quote="Left"), cid="MRI_LUMBAR.laterality"),
        finding(Status.MET, cid="MRI_LUMBAR.neuro_exam"),
    )
    assert [f.status for f in out.findings] == [Status.INSUFFICIENT] * 3
    assert [i.reason for i in report.invalid] == ["page_out_of_range", "too_short"]
    assert len("left") < MIN_QUOTE_CHARS


def test_one_bad_citation_invalidates_the_finding() -> None:
    out, _ = run(
        finding(
            Status.MET,
            Evidence(page=2, quote="radiating to the left leg for 9 weeks"),
            Evidence(page=2, quote="radiating to the right leg"),
        )
    )
    assert out.findings[0].status is Status.INSUFFICIENT


def test_insufficient_findings_keep_their_status() -> None:
    out, _ = run(finding(Status.INSUFFICIENT))
    assert out.findings[0].status is Status.INSUFFICIENT and not out.findings[0].verified


def test_injection_evidence_is_counted_but_flag_is_never_dropped() -> None:
    cf = CaseFindings(
        procedure_code="MRI_LUMBAR",
        findings=(),
        injection_detected=True,
        injection_evidence=(Evidence(page=1, quote="this text is not on the page"),),
    )
    out, report = verify(CASE, cf)
    assert out.injection_detected
    assert report.n_quotes == 1 and report.n_valid == 0
