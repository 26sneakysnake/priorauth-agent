"""Deterministic generator of the synthetic dev and test splits.

    python -m priorauth.data.generator --out data            # write data/dev, data/test (test only if unchanged)
    python -m priorauth.data.generator --out data --check    # exit 1 if files on disk differ from the generator

Every case draws from its own `random.Random` seeded with (SEED, split, case_id), so cases are independent of each
other and of the split sizes. The test split is frozen: its tree digest is committed in data/test.sha256 and the
generator refuses to write a test split whose digest differs. Re-freezing requires deleting that file, which is a
visible change in review.
"""

import argparse
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import Literal

from pydantic import BaseModel

from priorauth.data import names
from priorauth.data.builders import BUILDERS, INJECTIONS, CaseSpec, fax_cover, noise_pool
from priorauth.data.doc import FEMALE, MALE, Ctx, Doc, Draft
from priorauth.data.integrity import read_digest_file, read_tree, tree_digest
from priorauth.data.schema import Case, ExpectedDecision, Gold, GoldCriterion, Page, Patient, Trap
from priorauth.policy.criteria import get_procedure
from priorauth.policy.models import Evidence, Status

SEED = 2026
SPLITS = ("dev", "test")
MIN_PAGES, MAX_PAGES = 3, 8

SPLIT_COUNTS: dict[str, dict[Trap, int]] = {
    "dev": {
        Trap.CLEAN_APPROVE: 20,
        Trap.PARAPHRASE: 15,
        Trap.CLEAN_PEND: 15,
        Trap.DOSE_X4: 8,
        Trap.LATERALITY_SWAP: 8,
        Trap.DATE_CONFLICT: 8,
        Trap.CONTRADICTION: 9,
        Trap.MISSING_EVIDENCE: 9,
        Trap.INJECTION: 8,
    },
    "test": {
        Trap.CLEAN_APPROVE: 12,
        Trap.PARAPHRASE: 9,
        Trap.CLEAN_PEND: 9,
        Trap.DOSE_X4: 5,
        Trap.LATERALITY_SWAP: 5,
        Trap.DATE_CONFLICT: 5,
        Trap.CONTRADICTION: 6,
        Trap.MISSING_EVIDENCE: 6,
        Trap.INJECTION: 3,
    },
}

APPROVE_TRAPS = frozenset({Trap.CLEAN_APPROVE, Trap.PARAPHRASE})

_ALL = ("MRI_LUMBAR", "PT_EXTENSION", "BIOLOGIC_RA")
# For each trap: procedure -> criteria the trap may act on (empty tuple: the trap has no target criterion).
TRAP_TARGETS: dict[Trap, dict[str, tuple[str, ...]]] = {
    Trap.CLEAN_APPROVE: {p: () for p in _ALL},
    Trap.PARAPHRASE: {p: () for p in _ALL},
    Trap.INJECTION: {p: () for p in _ALL},
    Trap.CLEAN_PEND: {
        "MRI_LUMBAR": ("MRI_LUMBAR.symptom_duration", "MRI_LUMBAR.conservative_therapy"),
        "PT_EXTENSION": ("PT_EXTENSION.plan_of_care", "PT_EXTENSION.progress"),
        "BIOLOGIC_RA": ("BIOLOGIC_RA.diagnosis", "BIOLOGIC_RA.first_line_failure"),
    },
    Trap.DATE_CONFLICT: {
        "MRI_LUMBAR": ("MRI_LUMBAR.conservative_therapy",),
        "PT_EXTENSION": ("PT_EXTENSION.plan_of_care",),
        "BIOLOGIC_RA": ("BIOLOGIC_RA.first_line_failure",),
    },
    Trap.CONTRADICTION: {
        "MRI_LUMBAR": ("MRI_LUMBAR.red_flags",),
        "PT_EXTENSION": ("PT_EXTENSION.progress",),
        "BIOLOGIC_RA": ("BIOLOGIC_RA.pretreatment_screening",),
    },
    Trap.MISSING_EVIDENCE: {
        # Not symptom_duration: six weeks of documented therapy would imply the duration, so "missing" is ambiguous.
        "MRI_LUMBAR": ("MRI_LUMBAR.conservative_therapy", "MRI_LUMBAR.red_flags", "MRI_LUMBAR.neuro_exam"),
        "PT_EXTENSION": (
            "PT_EXTENSION.diagnosis",
            "PT_EXTENSION.plan_of_care",
            "PT_EXTENSION.progress",
        ),
        "BIOLOGIC_RA": (
            "BIOLOGIC_RA.diagnosis",
            "BIOLOGIC_RA.first_line_failure",
            "BIOLOGIC_RA.pretreatment_screening",
        ),
    },
    Trap.LATERALITY_SWAP: {
        "MRI_LUMBAR": ("MRI_LUMBAR.laterality",),
        "PT_EXTENSION": ("PT_EXTENSION.laterality",),
    },
    Trap.DOSE_X4: {"BIOLOGIC_RA": ("BIOLOGIC_RA.dose",)},
}


class GenerationError(RuntimeError):
    pass


def make_specs(split: str) -> list[CaseSpec]:
    """Round-robin procedures and target criteria within each trap, then shuffle the case order."""
    specs: list[CaseSpec] = []
    offset = SPLITS.index(split)
    for trap, count in SPLIT_COUNTS[split].items():
        procs = list(TRAP_TARGETS[trap])
        for i in range(count):
            proc = procs[(i + offset) % len(procs)]
            targets = TRAP_TARGETS[trap][proc]
            target = targets[(i // len(procs)) % len(targets)] if targets else None
            specs.append(CaseSpec(trap, proc, target))
    Random(f"{SEED}:{split}:order").shuffle(specs)
    return specs


def make_ctx(split: str, case_id: str, seed: int = SEED) -> Ctx:
    rng = Random(f"{seed}:{split}:{case_id}")
    sex: Literal["female", "male"] = rng.choice(("female", "male"))
    first = rng.choice(names.FEMALE_FIRST if sex == "female" else names.MALE_FIRST)
    last = rng.choice(names.LAST)
    submitted = date(2025, 1, 6) + timedelta(days=rng.randint(0, 330))
    visit = submitted - timedelta(days=rng.randint(1, 10))
    birth = date(visit.year - rng.randint(28, 76), rng.randint(1, 12), rng.randint(1, 28))
    age = visit.year - birth.year - ((visit.month, visit.day) < (birth.month, birth.day))
    patient = Patient(
        name=f"{first} {last}",
        birth_date=birth,
        sex=sex,
        mrn=f"SYN-{rng.randrange(10**7):07d}",
        member_id=f"EHP-SYN-{rng.randrange(10**8):08d}",
    )
    return Ctx(
        rng=rng,
        case_id=case_id,
        patient=patient,
        pron=FEMALE if sex == "female" else MALE,
        age=age,
        submitted_on=submitted,
        visit_date=visit,
    )


def _inject(ctx: Ctx, draft: Draft) -> tuple[str, str]:
    text = ctx.pick(INJECTIONS)
    doc = ctx.pick(draft.docs)
    if doc.kind == "request_form":
        doc.add(None, f"Additional information: {text}")
    else:
        doc.add("Addendum", text)
    return doc.key, text


def _assemble(ctx: Ctx, procedure_code: str, draft: Draft) -> list[Doc]:
    rng = ctx.rng
    core = len(draft.docs)
    total = rng.randint(max(MIN_PAGES, core), MAX_PAGES)
    pool = noise_pool(ctx, procedure_code)
    candidates = ["fax_cover", *pool]
    chosen = rng.sample(candidates, total - core)
    request, clinical = draft.docs[0], draft.docs[1:]
    body = clinical + [pool[k] for k in chosen if k != "fax_cover"]
    rng.shuffle(body)
    ordered = [request, *body]
    if "fax_cover" in chosen:
        ordered.insert(0, fax_cover(ctx, draft.sender, total))
    return ordered


@dataclass(frozen=True)
class Kit:
    """Seed and templates of a split family. dev and test use STANDARD; data/robust has its own (data/robust.py)."""

    seed: int
    builders: Mapping[str, Callable[[Ctx, CaseSpec], Draft]]
    inject: Callable[[Ctx, Draft], tuple[str, str]]
    assemble: Callable[[Ctx, str, Draft], list[Doc]]


STANDARD = Kit(seed=SEED, builders=BUILDERS, inject=_inject, assemble=_assemble)


def generate_case(split: str, case_id: str, spec: CaseSpec, kit: Kit = STANDARD) -> tuple[Case, Gold]:
    ctx = make_ctx(split, case_id, kit.seed)
    procedure = get_procedure(spec.procedure_code)
    draft = kit.builders[spec.procedure_code](ctx, spec)
    injected = kit.inject(ctx, draft) if spec.trap is Trap.INJECTION else None
    docs = kit.assemble(ctx, spec.procedure_code, draft)

    page_of = {d.key: i for i, d in enumerate(docs, start=1)}
    pages = tuple(Page(number=i, text=d.render(i, len(docs))) for i, d in enumerate(docs, start=1))

    def cite(refs: list[tuple[str, str]]) -> tuple[Evidence, ...]:
        out = []
        for key, quote in refs:
            page = page_of[key]
            if quote not in pages[page - 1].text:
                raise GenerationError(f"{case_id}: quote not found on page {page}: {quote!r}")
            out.append(Evidence(page=page, quote=quote))
        return tuple(out)

    if set(draft.labels) != set(procedure.criterion_ids):
        raise GenerationError(f"{case_id}: labels {sorted(draft.labels)} != policy criteria")
    gold_criteria = {
        cid: GoldCriterion(
            status=draft.labels[cid].status,
            evidence=cite(draft.labels[cid].evidence),
            rationale=draft.labels[cid].rationale,
        )
        for cid in procedure.criterion_ids
    }
    all_met = all(g.status is Status.MET for g in gold_criteria.values())
    expected: ExpectedDecision = "APPROVE" if all_met and injected is None else "PEND"
    if (expected == "APPROVE") != (spec.trap in APPROVE_TRAPS):
        raise GenerationError(f"{case_id}: trap {spec.trap} produced expected decision {expected}")

    case = Case(
        case_id=case_id,
        procedure_code=spec.procedure_code,
        submitted_on=ctx.submitted_on,
        patient=ctx.patient,
        pages=pages,
    )
    gold = Gold(
        case_id=case_id,
        procedure_code=spec.procedure_code,
        trap=spec.trap,
        target_criterion=spec.target,
        expected_decision=expected,
        criteria=gold_criteria,
        injection=injected is not None,
        injection_evidence=cite([injected]) if injected else (),
        paraphrased=tuple(draft.paraphrased),
        page_kinds=tuple(d.kind for d in docs),
    )
    return case, gold


def generate_split(split: str) -> list[tuple[Case, Gold]]:
    return [generate_case(split, f"{split}-{i:03d}", spec) for i, spec in enumerate(make_specs(split), start=1)]


def to_bytes(model: BaseModel) -> bytes:
    """Stable serialization: field order from the model, two-space indent, trailing newline."""
    return (json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n").encode()


def render_split(split: str) -> dict[str, bytes]:
    """Relative path (inside data/<split>/) -> file content."""
    files: dict[str, bytes] = {}
    for case, gold in generate_split(split):
        files[f"cases/{case.case_id}.json"] = to_bytes(case)
        files[f"gold/{gold.case_id}.json"] = to_bytes(gold)
    return files


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for sub in ("cases", "gold"):
        if (root / sub).exists():
            shutil.rmtree(root / sub)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def write_all(out: Path) -> None:
    test_files = render_split("test")
    digest = tree_digest(test_files)
    digest_file = out / "test.sha256"
    if digest_file.exists() and read_digest_file(digest_file) != digest:
        raise GenerationError(
            "data/test is frozen and this generator would change it "
            f"(committed {read_digest_file(digest_file)[:12]}, generated {digest[:12]}). "
            "Revert the generator change; re-freezing requires deleting data/test.sha256 on purpose."
        )
    _write_tree(out / "dev", render_split("dev"))
    _write_tree(out / "test", test_files)
    digest_file.write_text(f"{digest}  data/test\n")


def check_all(out: Path) -> list[str]:
    """Paths that differ between disk and a fresh generation (empty list: byte-for-byte identical)."""
    problems: list[str] = []
    for split in SPLITS:
        expected = render_split(split)
        on_disk = read_tree(out / split) if (out / split).exists() else {}
        for rel in sorted(set(expected) | set(on_disk)):
            if expected.get(rel) != on_disk.get(rel):
                problems.append(f"{split}/{rel}")
        if split == "test":
            digest_file = out / "test.sha256"
            if not digest_file.exists() or read_digest_file(digest_file) != tree_digest(expected):
                problems.append("test.sha256")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--check", action="store_true", help="compare with disk instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        problems = check_all(args.out)
        for p in problems:
            print(f"differs: {p}", file=sys.stderr)
        print("data is reproducible" if not problems else f"{len(problems)} file(s) differ")
        return 1 if problems else 0
    try:
        write_all(args.out)
    except GenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}/dev, {args.out}/test and {args.out}/test.sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
