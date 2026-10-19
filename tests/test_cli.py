"""The `priorauth` command line: policies, decisions replayed from the recorded responses, fail-closed exits."""

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from priorauth import cli
from priorauth.api.fhir import case_to_bundle
from priorauth.data.schema import Case
from priorauth.policy import criteria as pol

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "dev" / "cases"
CASSETTES = ROOT / "eval" / "cassettes"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def decide(capsys: pytest.CaptureFixture[str], path: Path, *extra: str) -> tuple[int, str, str]:
    return run(capsys, "decide", str(path), "--cassettes", str(CASSETTES), *extra)


def test_the_console_script_is_installed() -> None:
    (script,) = [e for e in entry_points(group="console_scripts") if e.name == "priorauth"]
    assert script.value == "priorauth.cli:main"


def test_policies_list_shows_every_procedure_and_criterion(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "policies", "list")
    assert code == 0 and pol.PAYER_NAME in out
    for proc in pol.PROCEDURES.values():
        assert proc.policy_id in out and all(cid in out for cid in proc.criterion_ids)


def test_policies_check_accepts_the_packaged_file_and_refuses_a_wrong_one(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _ = run(capsys, "policies", "check")
    assert code == 0 and "3 procedures, 13 criteria" in out
    wrong = tmp_path / "policies.yaml"
    wrong.write_text(pol.POLICY_FILE.read_text(encoding="utf-8").replace("{MAX_PLAN_WEEKS}", "{MAX_PLAN_DAYS}"))
    code, _, err = run(capsys, "policies", "check", str(pol.POLICY_FILE), str(wrong))
    assert code == 1 and "unknown placeholder {MAX_PLAN_DAYS}" in err and "MAX_PLAN_WEEKS is never used" in err


def test_decide_approves_a_clean_record_with_its_citations(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = decide(capsys, CASES / "dev-001.json")  # clean-approve-pt in the demo
    assert (code, err) == (0, "")
    assert "decision: APPROVE" in out and "reasons:" not in out
    assert out.count("p.") >= len(pol.PT_EXTENSION.criteria)  # one verified quote per criterion


@pytest.mark.parametrize(
    ("case_id", "reason"),
    [("dev-005", "NOT_MET"), ("dev-042", "INJECTION_DETECTED")],  # dose x4; hidden instruction
)
def test_decide_pends_the_traps_with_the_reason(capsys: pytest.CaptureFixture[str], case_id: str, reason: str) -> None:
    code, out, _ = decide(capsys, CASES / f"{case_id}.json")
    assert code == 0 and "decision: PEND" in out
    assert reason in out.split("reasons:")[1]


def test_decide_a_bundle_gives_the_same_run_as_the_case_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    case = Case.model_validate_json((CASES / "dev-005.json").read_bytes())
    bundle = tmp_path / "dev-005.json"
    bundle.write_text(json.dumps(case_to_bundle(case, case.case_id)))
    _, from_case, _ = decide(capsys, CASES / "dev-005.json", "--json")
    _, from_bundle, _ = decide(capsys, bundle, "--json")
    assert json.loads(from_case)["decision"] == json.loads(from_bundle)["decision"]
    assert json.loads(from_case)["decision"]["outcome"] == "PEND"


def test_decide_without_a_recording_fails_closed(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    case = Case.model_validate_json((CASES / "dev-001.json").read_bytes())
    pages = tuple(p.model_copy(update={"text": p.text + " Edited."}) for p in case.pages)  # what the model sees too
    edited = tmp_path / "edited.json"
    edited.write_text(case.model_copy(update={"pages": pages}).model_dump_json())
    code, out, err = decide(capsys, edited)
    assert code == 2 and "decision: PEND" in out
    assert "CassetteMiss" in err and "fail closed" in err


def test_decide_refuses_a_bundle_that_is_not_tagged_synthetic(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    case = Case.model_validate_json((CASES / "dev-001.json").read_bytes())
    bundle = case_to_bundle(case, case.case_id)
    bundle["meta"]["tag"] = []
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    code, out, err = decide(capsys, path)
    assert code == 1 and out == "" and "synthetic data only" in err


def test_decide_refuses_a_file_that_is_not_a_case(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text('{"text": "not a case"}')
    code, out, _ = decide(capsys, path)
    assert code == 1 and out == ""
