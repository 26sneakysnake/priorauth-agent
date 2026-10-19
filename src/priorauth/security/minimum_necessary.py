"""Minimum necessary (the principle of 45 CFR 164.502(b)): only pages that can matter to a criterion go to the model.

The rule is a conservative deny-list. A page is withheld only when all three hold:
1. its heading names an administrative document type (fax cover, scheduling note, eligibility check, intake
   questionnaire, refill request);
2. it mentions nothing from the clinical vocabulary of the requested procedure (sender and recipient lines aside);
3. it carries no instruction-like text (a prompt injection on a withheld page would never be flagged).
Every other page is sent, including document types the rule does not know. Withholding a page that mattered would
hide evidence or a contradiction from the model, so the rule errs towards sending; if it would withhold every
page, all pages are sent.

The headings, vocabularies and cues were written from data/dev only. eval/minimum_necessary.py measures, per split,
the share of pages sent and whether every gold evidence page and injection page was sent
(results/minimum_necessary.json).
"""

import re

from priorauth.data.schema import Case

ADMINISTRATIVE_HEADINGS = (
    "fax cover sheet",
    "scheduling note",
    "insurance eligibility verification",
    "patient intake questionnaire",
    "refill request",
)
HEADING_LINES = 3
# Sender and recipient lines name facilities ("From: Ashford Arthritis and Rheumatology"), not clinical facts.
ADDRESS_LINE = re.compile(r"^\s*(?:from|to):", re.IGNORECASE | re.MULTILINE)

VOCABULARY: dict[str, tuple[str, ...]] = {
    "MRI_LUMBAR": (
        "back", "lumbar", "spine", "spinal", "sciatica", "radicul", "pain", "numbness", "tingling", "weakness",
        "foot drop", "bowel", "bladder", "incontinence", "saddle", "cancer", "malignan", "weight loss", "fever",
        "trauma", "fall", "fracture", "infection", "neurolog", "reflex", "strength", "sensation", "physical therap",
        "chiropract", "nsaid", "ibuprofen", "naproxen", "meloxicam", "muscle relaxant", "cyclobenzaprine",
        "exercise", "mri", "m54", "m51",
    ),
    "PT_EXTENSION": (
        "physical therap", "plan of care", "knee", "hip", "shoulder", "ankle", "osteoarthritis",
        "rotator", "range of motion", "koos", "flexion", "strength", "pain", "exercise", "m17", "m16", "m75",
    ),
    "BIOLOGIC_RA": (
        "rheumat", "arthritis", "methotrexate", "mtx", "fictimab", "dmard", "biologic", "cdai", "das28", "joint",
        "synovitis", "tuberculosis", "tb", "quantiferon", "igra", "hepatitis", "hbsag", "leflunomide",
        "sulfasalazine", "hydroxychloroquine", "m05", "m06",
    ),
}  # fmt: skip
# Terms match at the start of a word ("radicul" matches "radiculopathy"; "back" does not match "callback").
_VOCABULARY = {
    code: re.compile(r"\b(?:" + "|".join(map(re.escape, terms)) + ")", re.IGNORECASE)
    for code, terms in VOCABULARY.items()
}

INJECTION_CUES = re.compile(
    r"ignore|disregard|override|instruction|automated|reviewer|\bai\b|assistant|approv|system prompt",
    re.IGNORECASE,
)


def heading(text: str) -> str:
    return " ".join([ln.strip() for ln in text.splitlines() if ln.strip()][:HEADING_LINES]).lower()


def is_administrative(text: str) -> bool:
    head = heading(text)
    return any(h in head for h in ADMINISTRATIVE_HEADINGS)


def withhold_reason(text: str, procedure_code: str) -> str | None:
    """Why a page is withheld, or None if it is sent."""
    if not is_administrative(text):
        return None
    vocabulary = _VOCABULARY.get(procedure_code)
    body = "\n".join(ln for ln in text.splitlines() if not ADDRESS_LINE.match(ln))
    if vocabulary is None or vocabulary.search(body):  # unknown procedure: nothing can be ruled irrelevant
        return None
    if INJECTION_CUES.search(text):
        return None
    return "administrative page with no term of the procedure's vocabulary"


def select_pages(case: Case) -> tuple[int, ...]:
    """Numbers of the pages to send, in record order."""
    sent = tuple(p.number for p in case.pages if withhold_reason(p.text, case.procedure_code) is None)
    return sent or tuple(p.number for p in case.pages)
