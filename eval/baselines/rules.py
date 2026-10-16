"""Baseline 1: keyword and regular-expression rules, no LLM.

What a careful engineer would write in an afternoon: look for the standard phrasing of each fact, compare stated
numbers with the policy thresholds, compare sides between the request and the notes, treat a non-negated red-flag
symptom or a "no improvement" statement as contradicting a positive one, and flag common prompt-injection phrases.
It does no date arithmetic and does not understand numbers written in words. Each finding cites the sentence that
triggered it, so its citations are verifiable like any other system's.

Developed against data/dev only. Caveat: the same person wrote the data templates, so the standard phrasing these
rules expect is exactly the phrasing the generator uses; real records would be harder for this baseline.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from priorauth.data.schema import Case
from priorauth.pipeline import Extraction, Usage
from priorauth.policy import criteria as pol
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status

MET, NOT_MET, INSUFFICIENT = Status.MET, Status.NOT_MET, Status.INSUFFICIENT


@dataclass(frozen=True)
class Hit:
    page: int
    sentence: str
    match: re.Match[str]
    on_request: bool

    @property
    def evidence(self) -> Evidence:
        return Evidence(page=self.page, quote=self.sentence)


def _sentence(line: str, start: int, end: int) -> str:
    """The sentence of `line` that contains [start, end), as an exact substring."""
    left = line.rfind(". ", 0, start)
    right = line.find(". ", end)
    return line[0 if left < 0 else left + 2 : len(line) if right < 0 else right + 1].strip()


def find(case: Case, pattern: re.Pattern[str]) -> list[Hit]:
    hits = []
    for page in case.pages:
        on_request = "PRIOR AUTHORIZATION REQUEST" in page.text
        for line in page.text.splitlines():
            for m in pattern.finditer(line):
                hits.append(Hit(page.number, _sentence(line, m.start(), m.end()), m, on_request))
    return hits


def rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


NEGATION = rx(r"\b(?:denies|denied|negative|no|without)\b")


def finding(
    cid: str, status: Status, hits: list[Hit] | tuple[Hit, ...] = (), *, conflict: bool = False, note: str = ""
) -> Finding:
    unique = list(dict.fromkeys(h.evidence for h in hits))
    return Finding(criterion_id=cid, status=status, evidence=tuple(unique), conflict=conflict, note=note)


def side_match(cid: str, request: list[Hit], notes: list[Hit]) -> Finding:
    """Compare the side named on the request with the sides named in the clinical notes."""
    if not request or not notes:
        return finding(cid, INSUFFICIENT, note="side missing on the request or in the notes")
    req = request[0].match.group(1).lower()
    sides = {h.match.group(1).lower() for h in notes}
    if sides == {req}:
        return finding(cid, MET, [request[0], notes[0]])
    if req not in sides:
        return finding(cid, NOT_MET, [request[0], notes[0]], note=f"request {req}, notes {sorted(sides)}")
    return finding(cid, INSUFFICIENT, [request[0], *notes], conflict=True, note="notes name both sides")


# --- lumbar MRI -----------------------------------------------------------------------------------------------

DURATION = rx(r"\b(?:pain|symptoms)\b[^.]*?\bfor (\d+) (weeks|months)\b")
CT_NONE = rx(r"\bno (?:physical therapy|PT)\b")
CT_STARTED = rx(r"\bstarted (?:physical therapy|PT) (\d+) weeks ago\b")
CT_DONE = rx(r"\b(\d+) weeks of (?:physical therapy|PT)\b")
RF_SCREEN = rx(r"\bred flag screen negative\b|\bdenies bowel or bladder\b")
RF_SYMPTOM = rx(
    r"\b(?:urinary (?:incontinence|retention)|bowel incontinence|saddle (?:anesthesia|numbness)|fever|"
    r"weight loss|history of cancer)\b"
)
NEURO = rx(r"\bstrength \d/5\b.*\breflex")
MRI_REQ_SIDE = rx(r"^Clinical indication: (Left|Right)\b")
MRI_NOTE_SIDE = rx(r"\b(left|right) leg\b")


def mri(case: Case) -> list[Finding]:
    out = []

    cid = "MRI_LUMBAR.symptom_duration"
    hits = find(case, DURATION)
    if hits:
        n, unit = int(hits[0].match.group(1)), hits[0].match.group(2).lower()
        weeks = n * 4 if unit == "months" else n
        out.append(finding(cid, MET if weeks >= pol.MIN_SYMPTOM_WEEKS else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no 'for N weeks' statement"))

    cid = "MRI_LUMBAR.conservative_therapy"
    if hits := find(case, CT_NONE):
        out.append(finding(cid, NOT_MET, hits[:1]))
    elif (hits := find(case, CT_STARTED)) or (hits := find(case, CT_DONE)):
        weeks = int(hits[0].match.group(1))
        out.append(finding(cid, MET if weeks >= pol.MIN_CONSERVATIVE_WEEKS else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no therapy statement"))

    cid = "MRI_LUMBAR.red_flags"
    screen = find(case, RF_SCREEN)
    positive = [h for h in find(case, RF_SYMPTOM) if not NEGATION.search(h.sentence)]
    if screen and positive:
        out.append(finding(cid, INSUFFICIENT, [screen[0], positive[0]], conflict=True, note="screen vs reported flag"))
    elif positive:
        out.append(finding(cid, NOT_MET, positive[:1]))
    elif screen:
        out.append(finding(cid, MET, screen[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no red-flag screen"))

    cid = "MRI_LUMBAR.neuro_exam"
    hits = find(case, NEURO)
    out.append(finding(cid, MET, hits[:1]) if hits else finding(cid, INSUFFICIENT, note="no neuro exam"))

    req = [h for h in find(case, MRI_REQ_SIDE) if h.on_request]
    notes = [h for h in find(case, MRI_NOTE_SIDE) if not h.on_request]
    out.append(side_match("MRI_LUMBAR.laterality", req, notes))
    return out


# --- physical therapy continuation ----------------------------------------------------------------------------

ICD10 = rx(r"\b([A-TV-Z])\d{2}\.\d{1,4}\b")
PLAN = rx(r"\b(\d+) visits per week for (\d+) weeks\b")
PROGRESS_POS = rx(r"\bimproved from (\d+) to (\d+)\b")
PROGRESS_NEG = rx(r"\bno measurable change\b|\bno improvement\b|\bunchanged\b")
PT_REQ_SIDE = rx(r"^Body part / side: (Left|Right) (?:knee|hip|shoulder)\b")
PT_NOTE_SIDE = rx(r"\b(left|right) (?:knee|hip|shoulder)\b")


def pt(case: Case) -> list[Finding]:
    out = []

    cid = "PT_EXTENSION.diagnosis"
    codes = find(case, ICD10)
    on_req = [h for h in codes if h.on_request]
    in_notes = [h for h in codes if not h.on_request]
    if on_req and in_notes:
        msk = all(h.match.group(1).upper() in "MS" for h in (on_req[0], in_notes[0]))
        out.append(finding(cid, MET if msk else NOT_MET, [on_req[0], in_notes[0]]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="ICD-10 code missing on the request or in the notes"))

    cid = "PT_EXTENSION.plan_of_care"
    hits = find(case, PLAN)
    if hits:
        weeks = int(hits[0].match.group(2))
        out.append(finding(cid, MET if weeks <= pol.MAX_PLAN_WEEKS else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no 'N visits per week for N weeks' statement"))

    cid = "PT_EXTENSION.progress"
    neg = find(case, PROGRESS_NEG)
    pos = [h for h in find(case, PROGRESS_POS) if not PROGRESS_NEG.search(h.sentence)]
    if pos and neg:
        out.append(finding(cid, INSUFFICIENT, [pos[0], neg[0]], conflict=True, note="gains vs no improvement"))
    elif neg:
        out.append(finding(cid, NOT_MET, neg[:1]))
    elif pos:
        out.append(finding(cid, MET, pos[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no measurable progress statement"))

    req = [h for h in find(case, PT_REQ_SIDE) if h.on_request]
    notes = [h for h in find(case, PT_NOTE_SIDE) if not h.on_request]
    out.append(side_match("PT_EXTENSION.laterality", req, notes))
    return out


# --- biologic for rheumatoid arthritis ------------------------------------------------------------------------

CDAI = rx(r"\bCDAI (\d+(?:\.\d+)?)")
DAS28 = rx(r"\bDAS28(?:-CRP|-ESR)? (\d+(?:\.\d+)?)")
MTX_COURSE = rx(r"\bmethotrexate (\d+) mg weekly for (\d+) weeks\b")
MTX_INTOLERANCE = rx(r"\bmethotrexate\b[^.]*\bintolerance\b")
MTX_STARTED = rx(r"\bmethotrexate (\d+) mg weekly started (\d+) weeks ago\b")
MTX_NONE = rx(r"\bno prior conventional DMARD\b")
DOSE = rx(r"^Dose: (\d+) mg subcutaneously every (\d+) days\b")
TB = rx(r"\bQuantiFERON[^:]*: (NEGATIVE|POSITIVE)\b")
HBV = rx(r"\bHepatitis B surface antigen: (NON-REACTIVE|REACTIVE)\b")
SCREEN_CLAIM = rx(r"\bscreens? negative\b")


def bio(case: Case) -> list[Finding]:
    out = []

    cid = "BIOLOGIC_RA.diagnosis"
    if hits := find(case, CDAI):
        out.append(finding(cid, MET if float(hits[0].match.group(1)) > pol.MIN_CDAI else NOT_MET, hits[:1]))
    elif hits := find(case, DAS28):
        out.append(finding(cid, MET if float(hits[0].match.group(1)) > pol.MIN_DAS28 else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no CDAI or DAS28"))

    cid = "BIOLOGIC_RA.first_line_failure"
    if hits := find(case, MTX_NONE):
        out.append(finding(cid, NOT_MET, hits[:1]))
    elif hits := find(case, MTX_INTOLERANCE):
        out.append(finding(cid, MET, hits[:1]))
    elif hits := find(case, MTX_STARTED):
        weeks = int(hits[0].match.group(2))
        out.append(finding(cid, MET if weeks >= pol.MIN_MTX_WEEKS else NOT_MET, hits[:1]))
    elif hits := find(case, MTX_COURSE):
        mg, weeks = int(hits[0].match.group(1)), int(hits[0].match.group(2))
        ok = weeks >= pol.MIN_MTX_WEEKS and mg >= pol.MIN_MTX_MG_WEEKLY
        out.append(finding(cid, MET if ok else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no methotrexate course"))

    cid = "BIOLOGIC_RA.dose"
    if hits := [h for h in find(case, DOSE) if h.on_request]:
        mg, every = int(hits[0].match.group(1)), int(hits[0].match.group(2))
        per_interval = mg * pol.DOSE_INTERVAL_DAYS / every
        out.append(finding(cid, MET if per_interval <= pol.MAX_MG_PER_INTERVAL else NOT_MET, hits[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no dose on the request"))

    cid = "BIOLOGIC_RA.pretreatment_screening"
    tb, hbv, claim = find(case, TB), find(case, HBV), find(case, SCREEN_CLAIM)
    tb_pos = [h for h in tb if h.match.group(1).upper() == "POSITIVE"]
    if tb_pos and claim:
        out.append(finding(cid, INSUFFICIENT, [claim[0], tb_pos[0]], conflict=True, note="note vs lab"))
    elif tb_pos:
        out.append(finding(cid, NOT_MET, tb_pos[:1]))
    elif tb and hbv and hbv[0].match.group(1).upper() == "NON-REACTIVE":
        out.append(finding(cid, MET, [tb[0], hbv[0]]))
    elif claim:
        out.append(finding(cid, MET, claim[:1]))
    else:
        out.append(finding(cid, INSUFFICIENT, note="no TB / hepatitis B result"))
    return out


# --- injection and the system wrapper -------------------------------------------------------------------------

INJECTION = rx(
    r"\b(?:ignore|disregard)\b[^.]{0,40}\b(?:criteria|instructions|policy)\b|"
    r"\b(?:AI|automated) review(?:er| system)\b|\bassistant override\b|\bsystem instruction\b"
)

RULES: dict[str, Callable[[Case], list[Finding]]] = {"MRI_LUMBAR": mri, "PT_EXTENSION": pt, "BIOLOGIC_RA": bio}


class RulesBaseline:
    name = "rules_baseline"

    async def extract(self, case: Case) -> Extraction:
        rules = RULES.get(case.procedure_code)
        findings = rules(case) if rules else []
        injections = find(case, INJECTION)
        return Extraction(
            findings=CaseFindings(
                procedure_code=case.procedure_code,
                findings=tuple(findings),
                injection_detected=bool(injections),
                injection_evidence=tuple(dict.fromkeys(h.evidence for h in injections)),
            ),
            usage=Usage(),
        )
