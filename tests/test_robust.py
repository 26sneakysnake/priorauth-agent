"""The robustness split: reproducible, frozen, self-consistent, and written with templates dev and test never use."""

import re
from collections import Counter
from functools import cache
from pathlib import Path
from random import Random

import pytest

from priorauth.data import robust
from priorauth.data.builders import INJECTIONS as STANDARD_INJECTIONS
from priorauth.data.doc import SYNTHETIC_FOOTER
from priorauth.data.generator import GenerationError
from priorauth.data.integrity import digest_dir, read_digest_file
from priorauth.data.schema import Case, Gold, Trap
from priorauth.policy.criteria import get_procedure
from priorauth.policy.models import Status
from tests.test_data import SECONDARY_EFFECTS
from tests.test_data import load as load_standard

DATA = Path(__file__).resolve().parents[1] / "data"


@cache
def cases() -> list[tuple[Case, Gold]]:
    out = []
    for path in sorted((DATA / "robust" / "cases").glob("*.json")):
        gold = Gold.model_validate_json((DATA / "robust" / "gold" / path.name).read_bytes())
        out.append((Case.model_validate_json(path.read_bytes()), gold))
    return out


def test_committed_robust_data_is_byte_for_byte_what_the_generator_produces() -> None:
    assert robust.check(DATA) == []


def test_robust_split_matches_its_committed_digest() -> None:
    assert digest_dir(DATA / "robust") == read_digest_file(DATA / "robust.sha256")


def test_generator_refuses_to_overwrite_a_frozen_robust_split(tmp_path: Path) -> None:
    (tmp_path / "robust.sha256").write_text("0" * 64 + "  data/robust\n")
    with pytest.raises(GenerationError, match="frozen"):
        robust.write(tmp_path)
    assert not (tmp_path / "robust").exists()


def test_distribution() -> None:
    assert Counter(g.trap for _, g in cases()) == Counter(robust.COUNTS)
    assert len(cases()) == 40
    assert sum(g.expected_decision == "PEND" for _, g in cases()) >= 20
    assert {g.procedure_code for _, g in cases()} == {"MRI_LUMBAR", "PT_EXTENSION", "BIOLOGIC_RA"}


def test_labels_are_consistent() -> None:
    for case, gold in cases():
        assert tuple(gold.criteria) == get_procedure(case.procedure_code).criterion_ids
        for ev in [e for g in gold.criteria.values() for e in g.evidence] + list(gold.injection_evidence):
            assert ev.quote in case.page(ev.page).text, (case.case_id, ev)
        for cid, g in gold.criteria.items():
            if g.status is not Status.INSUFFICIENT:
                assert g.evidence, (gold.case_id, cid)
        all_met = all(g.status is Status.MET for g in gold.criteria.values())
        assert (gold.expected_decision == "APPROVE") == (all_met and not gold.injection)
        assert gold.injection == (gold.trap is Trap.INJECTION)
        failing = {cid for cid, g in gold.criteria.items() if g.status is not Status.MET}
        if gold.target_criterion is None:
            assert failing == set(), gold.case_id
        else:
            allowed = {gold.target_criterion} | SECONDARY_EFFECTS.get((gold.trap, gold.target_criterion), set())
            assert gold.target_criterion in failing and failing <= allowed, gold.case_id
        if gold.trap is Trap.CONTRADICTION:
            assert gold.target_criterion is not None
            assert len({e.page for e in gold.criteria[gold.target_criterion].evidence}) == 2
        assert (len(gold.paraphrased) >= 2) == (gold.trap is Trap.PARAPHRASE)


def test_pages_are_numbered_footed_and_synthetic() -> None:
    leaks = [t.value for t in Trap] + [s.value for s in Status] + ["rationale", "expected_decision"]
    for case, gold in cases():
        n = len(case.pages)
        assert robust.MIN_PAGES <= n <= robust.MAX_PAGES
        assert [p.number for p in case.pages] == list(range(1, n + 1)) and len(gold.page_kinds) == n
        text = "\n".join(p.text for p in case.pages)
        for p in case.pages:
            assert f"[fax p.{p.number}/{n}] {SYNTHETIC_FOOTER}" in p.text
        assert case.patient.mrn.startswith("SYN-") and case.patient.member_id.startswith("EHP-SYN-")
        assert all(npi.startswith("9") for npi in re.findall(r"NPI: (\d{10})", text))
        for word in leaks:
            assert word not in case.model_dump_json(), (case.case_id, word)


def test_request_form_is_not_always_first() -> None:
    assert sum(g.page_kinds.index("request_form") > 1 for _, g in cases()) >= 10


def test_some_pages_carry_ocr_noise_and_it_spares_meaning() -> None:
    noisy = sum(
        "0" in p.text and re.search(r"[A-Za-z]0[A-Za-z]", p.text) is not None for c, _ in cases() for p in c.pages
    )
    assert noisy >= 5
    assert robust.ocr(Random(0), "denies fever, right leg, negative, 12 weeks", rate=1.0).split(" ")[0] == "denies"
    sample = robust.ocr(Random(1), "radiculopathy improved right 2025-01-01", rate=1.0)
    assert sample.split(" ")[1:] == ["improved", "right", "2025-01-01"]


def test_templates_are_new() -> None:
    """No gold evidence sentence of robust appears anywhere in dev or test, and the injections are new."""
    standard_text = "\n".join(p.text for split in ("dev", "test") for c, _ in load_standard(split) for p in c.pages)
    quotes = {e.quote for _, g in cases() for gc in g.criteria.values() for e in gc.evidence}
    reused = sorted(q for q in quotes if q in standard_text)
    assert reused == []
    assert set(robust.INJECTIONS).isdisjoint(STANDARD_INJECTIONS)
    assert "PRIOR AUTHORIZATION REQUEST" not in "\n".join(p.text for c, _ in cases() for p in c.pages)
