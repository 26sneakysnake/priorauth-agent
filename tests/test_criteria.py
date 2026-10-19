import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def test_the_policies_come_from_the_yaml_file_and_its_parameters_are_the_constants() -> None:
    loaded = pol.load_policies(pol.POLICY_FILE)
    assert pol.POLICY_FILE.suffix == ".yaml" and loaded == pol.POLICIES
    assert loaded.procedures == pol.PROCEDURES and loaded.payer == pol.PAYER_NAME
    for name, value in loaded.parameters.items():
        assert getattr(pol, name) == value and type(getattr(pol, name)) is type(value), name
    texts = [p.name for p in pol.PROCEDURES.values()]
    texts += [t for p in pol.PROCEDURES.values() for c in p.criteria for t in c.model_dump().values()]
    assert not [t for t in texts if "{" in t or "}" in t]  # every placeholder was filled in


def _minimal() -> dict[str, Any]:
    return {
        "payer": "Test payer (fictional)",
        "parameters": {"MIN_WEEKS": 6},
        "fragments": {"CONFLICT": "the record contradicts itself"},
        "procedures": [
            {
                "code": "X",
                "name": "Test procedure",
                "policy_id": "T-1",
                "criteria": [
                    {
                        "id": "X.duration",
                        "title": "Duration",
                        "requirement": "At least {MIN_WEEKS} weeks.",
                        "met_when": "{MIN_WEEKS} weeks or more",
                        "not_met_when": "less than {MIN_WEEKS} weeks",
                        "insufficient_when": "not documented, or {CONFLICT}",
                    }
                ],
            }
        ],
    }


def test_a_policy_file_fills_its_placeholders() -> None:
    criterion = pol.parse_policies(_minimal()).procedures["X"].criterion("X.duration")
    assert criterion.requirement == "At least 6 weeks."
    assert criterion.insufficient_when == "not documented, or the record contradicts itself"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["procedures"][0]["criteria"][0].update(title="{MAX_WEEKS}"), "unknown placeholder {MAX_WEEKS}"),
        (lambda d: d["parameters"].update(MAX_WEEKS=12), "MAX_WEEKS is never used"),
        (lambda d: d["parameters"].update(MIN_WEEKS=True), "parameters.MIN_WEEKS"),
        (lambda d: d["parameters"].update(min_weeks=1), "names are UPPER_SNAKE_CASE"),
        (lambda d: d["procedures"][0]["criteria"][0].update(title="{MIN_WEEKS:>4}"), "must be a bare name"),
        (lambda d: d["procedures"][0]["criteria"][0].update(id="Y.duration"), "start with the procedure code"),
        (lambda d: d["procedures"][0]["criteria"].append(d["procedures"][0]["criteria"][0]), "duplicate criterion"),
        (lambda d: d["procedures"].append(d["procedures"][0]), "duplicate procedure code"),
        (lambda d: d["procedures"][0].update(criteria=[]), "at least one criterion"),
        (lambda d: d["procedures"][0].update(threshold=3), "threshold"),
        (lambda d: d["fragments"].update(CONFLICT="see {MIN_WEEKS}"), "cannot contain placeholders"),
    ],
)
def test_a_wrong_policy_file_is_refused_with_the_reason(change: Callable[[dict[str, Any]], None], message: str) -> None:
    doc = _minimal()
    change(doc)
    with pytest.raises(pol.PolicyError, match=re.escape(message)):
        pol.parse_policies(doc)


def test_a_policy_file_that_is_not_yaml_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "policies.yaml"
    bad.write_text("payer: [unclosed\n", encoding="utf-8")
    with pytest.raises(pol.PolicyError, match=re.escape("policies.yaml")):
        pol.load_policies(bad)
