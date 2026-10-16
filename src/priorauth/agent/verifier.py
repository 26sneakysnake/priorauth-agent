"""Deterministic evidence verifier.

A citation is accepted only if its normalized quote is a substring of the normalized text of the page it cites.
A MET or NOT_MET finding whose citations are missing or not all accepted is downgraded to INSUFFICIENT: the policy
engine never sees an unverified positive claim. The verifier also counts invalid citations *before* any downgrade,
which is the "evidence validity" metric of the evaluation.
"""

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

from priorauth.data.schema import Case
from priorauth.policy.models import CaseFindings, Evidence, Finding, Status

# Quotes shorter than this (after normalization) match too easily to prove anything ("left", "negative").
MIN_QUOTE_CHARS = 12

# Typographic quotes, dashes and non-breaking space -> ASCII (written as code points to keep the source ASCII).
_TRANSLATE = str.maketrans(
    {
        **dict.fromkeys(map(chr, (0x2018, 0x2019)), "'"),
        **dict.fromkeys(map(chr, (0x201C, 0x201D)), '"'),
        **dict.fromkeys(map(chr, (0x2013, 0x2014, 0x2212)), "-"),
        chr(0x00A0): " ",
    }
)
_WS = re.compile(r"\s+")
_EDGE = " \t\n\"'.,;:" + chr(0x2026)


def normalize(text: str) -> str:
    """Case, Unicode form, typographic quotes/dashes and whitespace do not change what a quote says."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    return _WS.sub(" ", text).strip()


def normalize_quote(quote: str) -> str:
    """Like `normalize`, plus leading/trailing ellipses, quotation marks and punctuation are ignored."""
    q = normalize(quote)
    while q.startswith("...") or q.endswith("..."):
        q = q.removeprefix("...").removesuffix("...")
    return q.strip(_EDGE)


InvalidReason = Literal["page_out_of_range", "too_short", "not_found"]


class InvalidEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str | None  # None for injection evidence
    evidence: Evidence
    reason: InvalidReason


class VerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_quotes: int
    n_valid: int
    invalid: tuple[InvalidEvidence, ...]
    downgraded: tuple[str, ...]  # criterion ids turned into INSUFFICIENT


def check_evidence(case: Case, ev: Evidence) -> InvalidReason | None:
    if not 1 <= ev.page <= len(case.pages):
        return "page_out_of_range"
    quote = normalize_quote(ev.quote)
    if len(quote) < MIN_QUOTE_CHARS:
        return "too_short"
    if quote not in normalize(case.page(ev.page).text):
        return "not_found"
    return None


def verify(case: Case, findings: CaseFindings) -> tuple[CaseFindings, VerificationReport]:
    invalid: list[InvalidEvidence] = []
    downgraded: list[str] = []
    n_quotes = 0
    out: list[Finding] = []
    for f in findings.findings:
        problems = [(ev, check_evidence(case, ev)) for ev in f.evidence]
        n_quotes += len(problems)
        bad = [InvalidEvidence(criterion_id=f.criterion_id, evidence=ev, reason=r) for ev, r in problems if r]
        invalid.extend(bad)
        verified = bool(f.evidence) and not bad
        status, note = f.status, f.note
        if f.status in (Status.MET, Status.NOT_MET) and not verified:
            status = Status.INSUFFICIENT
            note = f"{f.status} claimed but evidence not verified" + (f"; {f.note}" if f.note else "")
            downgraded.append(f.criterion_id)
        out.append(f.model_copy(update={"status": status, "verified": verified, "note": note}))

    for ev in findings.injection_evidence:
        n_quotes += 1
        reason = check_evidence(case, ev)
        if reason:
            invalid.append(InvalidEvidence(criterion_id=None, evidence=ev, reason=reason))

    report = VerificationReport(
        n_quotes=n_quotes,
        n_valid=n_quotes - len(invalid),
        invalid=tuple(invalid),
        downgraded=tuple(downgraded),
    )
    # The injection flag is kept even if its quote is invalid: a doubtful safety signal still routes to a human.
    return findings.model_copy(update={"findings": tuple(out)}), report
