"""What the model view sends: share of pages sent, evidence kept, identifiers redacted. No model is called.

    make minimum-necessary      # -> results/minimum_necessary.json

For every case of every split, priorauth.security.model_view.model_view is computed and compared with the gold
labels: is every page that holds gold evidence (or a prompt injection) among the pages sent? How many identifier
occurrences were replaced, and does any known identifier of the patient survive in the text sent? The page rule was
written from data/dev only; test and robust are measured, never tuned on. Deterministic: regenerating gives the same
bytes (tests/test_security.py checks it).
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from eval.metrics import rate
from eval.run import load_split
from priorauth.data.schema import Case, Patient
from priorauth.security.model_view import model_view
from priorauth.security.redaction import known_identifiers

OUTPUT = Path("results/minimum_necessary.json")
SPLITS = ("dev", "test", "robust")


def residual_identifiers(patient: Patient, text: str) -> int:
    """Known identifiers still present: every literal the redactor looks for, plus the bare digits of MRN and
    member id (SYN-7835740 -> 7835740)."""
    literals = set(known_identifiers(patient))
    literals |= {re.sub(r"\D", "", x) for x in (patient.mrn, patient.member_id)}
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(literals, key=len, reverse=True))) + r")(?!\w)", re.IGNORECASE
    )
    return len(pattern.findall(text))


def measure(pairs: list[tuple[Case, Any]]) -> dict[str, Any]:
    pages = sent = 0
    ev_pages = ev_kept = inj_pages = inj_kept = cases_complete = residual = 0
    replaced: Counter[str] = Counter()
    for case, gold in pairs:
        view = model_view(case)
        kept = set(view.page_map)
        pages += len(case.pages)
        sent += len(kept)
        evidence = {e.page for gc in gold.criteria.values() for e in gc.evidence}
        injection = {e.page for e in gold.injection_evidence}
        ev_pages, ev_kept = ev_pages + len(evidence), ev_kept + len(evidence & kept)
        inj_pages, inj_kept = inj_pages + len(injection), inj_kept + len(injection & kept)
        cases_complete += (evidence | injection) <= kept
        replaced.update(view.redactor.replaced)
        residual += sum(residual_identifiers(case.patient, p.text) for p in view.case.pages)
    return {
        "cases": len(pairs),
        "pages": pages,
        "pages_sent": sent,
        "share_of_pages_sent": round(sent / pages, 4),
        "evidence_pages_sent": rate(ev_kept, ev_pages).model_dump(),
        "injection_pages_sent": rate(inj_kept, inj_pages).model_dump(),
        "cases_with_every_evidence_page_sent": rate(cases_complete, len(pairs)).model_dump(),
        "identifiers_replaced": dict(sorted(replaced.items())),
        "known_identifiers_left_in_text_sent": residual,
    }


def report(data: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "description": "Model view of every synthetic case (eval/minimum_necessary.py): pages selected by "
        "priorauth.security.minimum_necessary, identifiers replaced by priorauth.security.redaction. Rates carry "
        "95% Wilson intervals. The page rule was written from dev only. Generated, never edited by hand.",
        "splits": {split: measure(load_split(data, split)) for split in SPLITS},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    result = report(args.data)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for split, m in result["splits"].items():
        print(
            f"{split}: {m['pages_sent']}/{m['pages']} pages sent ({m['share_of_pages_sent']:.1%}), evidence pages "
            f"{m['evidence_pages_sent']['k']}/{m['evidence_pages_sent']['n']}, injection pages "
            f"{m['injection_pages_sent']['k']}/{m['injection_pages_sent']['n']}, identifiers left "
            f"{m['known_identifiers_left_in_text_sent']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
