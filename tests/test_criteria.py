import pytest

from priorauth.policy import criteria as pol


def test_three_procedures_with_four_to_six_criteria() -> None:
    assert set(pol.PROCEDURES) == {"MRI_LUMBAR", "PT_EXTENSION", "BIOLOGIC_RA"}
    for proc in pol.PROCEDURES.values():
        assert 4 <= len(proc.criteria) <= 6
        assert len(set(proc.criterion_ids)) == len(proc.criterion_ids)
        assert all(cid.startswith(f"{proc.code}.") for cid in proc.criterion_ids)


def test_policy_text_states_the_thresholds_used_in_code() -> None:
    duration = pol.MRI_LUMBAR.criterion("MRI_LUMBAR.symptom_duration").requirement
    assert f"at least {pol.MIN_SYMPTOM_WEEKS} weeks" in duration
    assert f"{pol.MAX_PLAN_WEEKS} weeks" in pol.PT_EXTENSION.criterion("PT_EXTENSION.plan_of_care").requirement
    dose = pol.BIOLOGIC_RA.criterion("BIOLOGIC_RA.dose").requirement
    assert f"{pol.MAX_MG_PER_INTERVAL} mg per {pol.DOSE_INTERVAL_DAYS} days" in dose


def test_unknown_procedure_or_criterion_raises() -> None:
    with pytest.raises(KeyError):
        pol.get_procedure("CT_HEAD")
    with pytest.raises(KeyError):
        pol.MRI_LUMBAR.criterion("MRI_LUMBAR.nope")
