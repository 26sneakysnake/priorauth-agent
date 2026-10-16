"""Acceptance tests for P1: reproducible, frozen, correctly distributed and self-consistent synthetic data."""

import re
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from priorauth.data import generator as gen
from priorauth.data.doc import SYNTHETIC_FOOTER, number_word
from priorauth.data.integrity import digest_dir, read_digest_file
from priorauth.data.schema import Case, Gold, Trap
from priorauth.policy.criteria import get_procedure
from priorauth.policy.models import Status

DATA = Path(__file__).resolve().parents[1] / "data"

# A trap may knock out more than its target criterion only where the timeline makes that unavoidable.
SECONDARY_EFFECTS = {(Trap.CLEAN_PEND, "MRI_LUMBAR.symptom_duration"): {"MRI_LUMBAR.conservative_therapy"}}


@cache
def load(split: str) -> list[tuple[Case, Gold]]:
    out = []
    for path in sorted((DATA / split / "cases").glob("*.json")):
        case = Case.model_validate_json(path.read_bytes())
        gold = Gold.model_validate_json((DATA / split / "gold" / path.name).read_bytes())
        out.append((case, gold))
    return out


def all_cases() -> list[tuple[Case, Gold]]:
    return load("dev") + load("test")


def test_committed_data_is_byte_for_byte_what_the_generator_produces() -> None:
    assert gen.check_all(DATA) == []


def test_generation_does_not_depend_on_previous_calls() -> None:
    assert gen.render_split("test") == gen.render_split("test")


def test_test_split_matches_committed_digest() -> None:
    assert digest_dir(DATA / "test") == read_digest_file(DATA / "test.sha256")


def test_generator_refuses_to_overwrite_a_frozen_test_split(tmp_path: Path) -> None:
    (tmp_path / "test.sha256").write_text("0" * 64 + "  data/test\n")
    with pytest.raises(gen.GenerationError, match="frozen"):
        gen.write_all(tmp_path)
    assert not (tmp_path / "dev").exists()
    assert not (tmp_path / "test").exists()


def test_generator_freezes_on_first_write_and_is_idempotent(tmp_path: Path) -> None:
    gen.write_all(tmp_path)
    first = (tmp_path / "test.sha256").read_text()
    gen.write_all(tmp_path)
    assert (tmp_path / "test.sha256").read_text() == first
    assert gen.check_all(tmp_path) == []


@pytest.mark.parametrize("split", gen.SPLITS)
def test_trap_distribution_matches_the_plan(split: str) -> None:
    traps = Counter(g.trap for _, g in load(split))
    assert traps == Counter(gen.SPLIT_COUNTS[split])
    assert len(load(split)) == {"dev": 100, "test": 60}[split]


def test_test_split_has_at_least_thirty_expected_pends() -> None:
    assert sum(g.expected_decision == "PEND" for _, g in load("test")) >= 30


@pytest.mark.parametrize("split", gen.SPLITS)
def test_every_allowed_procedure_appears_for_every_trap(split: str) -> None:
    seen = {(g.trap, g.procedure_code) for _, g in load(split)}
    for trap, procs in gen.TRAP_TARGETS.items():
        for proc in procs:
            if gen.SPLIT_COUNTS[split][trap] >= len(procs):
                assert (trap, proc) in seen, (split, trap, proc)


def test_every_gold_quote_exists_verbatim_in_its_page() -> None:
    n_quotes = 0
    for case, gold in all_cases():
        refs = [e for g in gold.criteria.values() for e in g.evidence] + list(gold.injection_evidence)
        for ev in refs:
            assert ev.quote in case.page(ev.page).text, (case.case_id, ev)
            n_quotes += 1
    assert n_quotes > 500


def test_gold_labels_cover_exactly_the_policy_criteria() -> None:
    for case, gold in all_cases():
        assert gold.procedure_code == case.procedure_code
        assert tuple(gold.criteria) == get_procedure(case.procedure_code).criterion_ids


def test_met_and_not_met_labels_always_cite_evidence() -> None:
    for _, gold in all_cases():
        for cid, g in gold.criteria.items():
            if g.status is not Status.INSUFFICIENT:
                assert g.evidence, (gold.case_id, cid)


def test_expected_decision_follows_from_labels_and_injection() -> None:
    for _, gold in all_cases():
        all_met = all(g.status is Status.MET for g in gold.criteria.values())
        assert (gold.expected_decision == "APPROVE") == (all_met and not gold.injection)
        assert (gold.expected_decision == "APPROVE") == (gold.trap in gen.APPROVE_TRAPS)
        assert gold.injection == (gold.trap is Trap.INJECTION)


def test_traps_only_affect_their_target_criterion() -> None:
    for _, gold in all_cases():
        failing = {cid for cid, g in gold.criteria.items() if g.status is not Status.MET}
        if gold.target_criterion is None:
            assert failing == set(), gold.case_id
        else:
            allowed = {gold.target_criterion} | SECONDARY_EFFECTS.get((gold.trap, gold.target_criterion), set())
            assert gold.target_criterion in failing, gold.case_id
            assert failing <= allowed, gold.case_id


def test_conflict_traps_are_insufficient_and_cite_both_sides() -> None:
    for _, gold in all_cases():
        if gold.trap in (Trap.CONTRADICTION, Trap.DATE_CONFLICT):
            assert gold.target_criterion is not None
            target = gold.criteria[gold.target_criterion]
            assert target.status is Status.INSUFFICIENT
            if gold.trap is Trap.CONTRADICTION:
                assert len({e.page for e in target.evidence}) == 2, gold.case_id


def test_paraphrase_cases_reword_at_least_two_criteria() -> None:
    for _, gold in all_cases():
        assert (len(gold.paraphrased) >= 2) == (gold.trap is Trap.PARAPHRASE)


def test_pages_are_numbered_footed_and_within_bounds() -> None:
    for case, gold in all_cases():
        n = len(case.pages)
        assert gen.MIN_PAGES <= n <= gen.MAX_PAGES
        assert [p.number for p in case.pages] == list(range(1, n + 1))
        assert len(gold.page_kinds) == n
        assert gold.page_kinds[0] in ("fax_cover", "request_form")
        for p in case.pages:
            assert f"Page {p.number} of {n} | {SYNTHETIC_FOOTER}" in p.text


def test_identifiers_are_visibly_synthetic() -> None:
    for case, _ in all_cases():
        assert case.synthetic is True
        assert case.patient.mrn.startswith("SYN-")
        assert case.patient.member_id.startswith("EHP-SYN-")
        text = "\n".join(p.text for p in case.pages)
        for npi in re.findall(r"NPI: (\d+)", text):
            assert len(npi) == 10 and npi.startswith("9")  # issued NPIs start with 1 or 2
        for phone in re.findall(r"\(\d{3}\) \d{3}-\d{4}", text):
            assert re.fullmatch(r"\(\d{3}\) 555-01\d\d", phone)


def test_case_files_do_not_leak_gold_labels() -> None:
    leaks = [t.value for t in Trap] + [s.value for s in Status] + ["rationale", "expected_decision"]
    for case, _ in all_cases():
        text = case.model_dump_json()
        for word in leaks:
            assert word not in text, (case.case_id, word)


def test_number_word() -> None:
    assert [number_word(n) for n in (0, 7, 13, 20, 22, 99)] == [
        "zero",
        "seven",
        "thirteen",
        "twenty",
        "twenty-two",
        "ninety-nine",
    ]
    with pytest.raises(ValueError):
        number_word(100)
