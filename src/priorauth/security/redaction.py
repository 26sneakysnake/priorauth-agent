"""De-identification of record text before any model call (a principle of 45 CFR 164.514, not Safe Harbor).

The patient's identifiers that the structured header announces (full name and each part of it, MRN, member id, date
of birth in the four formats the records use) and North American phone numbers found in free text are replaced by
placeholders: [PATIENT], [MRN], [MEMBER_ID], [DOB], [PHONE]. Each distinct spelling gets its own placeholder
([DOB], [DOB_2], ...), so the replacement is exactly reversible: quotes the model copies from a redacted page are
restored before the deterministic verifier checks them against the original page.

What is NOT removed: service dates, ages, provider and facility names, NPIs, addresses and any identifier the header
does not announce, or that OCR noise has misspelled. This is closer to a limited data set than to Safe Harbor
de-identification (164.514(b)(2)), which is not claimed. A page that already contains a placeholder-shaped token
would be restored wrongly; the verifier then rejects the quote and the case is pended (fail-closed).
"""

import re
from collections import Counter
from datetime import date

from priorauth.data.schema import Patient

CATEGORIES = ("PATIENT", "MRN", "MEMBER_ID", "DOB", "PHONE")
PLACEHOLDER = re.compile(r"\[(?:" + "|".join(CATEGORIES) + r")(?:_\d+)?\]")
PHONE = re.compile(r"(?<![\w-])(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?![\w-])")
MIN_NAME_PART = 3  # shorter name parts ("Al", initials) would hit ordinary words


def date_forms(d: date) -> tuple[str, ...]:
    """The spellings of a date in the synthetic records: 01/04/1959, 1959-01-04, January 4, 1959, 01-04-1959."""
    return (d.strftime("%m/%d/%Y"), d.isoformat(), f"{d.strftime('%B')} {d.day}, {d.year}", d.strftime("%m-%d-%Y"))


def known_identifiers(patient: Patient) -> dict[str, str]:
    """Lower-cased literal -> category, for every identifier of this patient the redactor looks for."""
    literals = {patient.name.lower(): "PATIENT"}
    for part in patient.name.split():
        if len(part) >= MIN_NAME_PART:
            literals.setdefault(part.lower(), "PATIENT")
    literals[patient.mrn.lower()] = "MRN"
    literals[patient.member_id.lower()] = "MEMBER_ID"
    for form in date_forms(patient.birth_date):
        literals[form.lower()] = "DOB"
    return literals


class Redactor:
    """Reversible redaction for one case. The same spelling always gets the same placeholder across pages."""

    def __init__(self, patient: Patient) -> None:
        self._literals = known_identifiers(patient)
        alternation = "|".join(re.escape(x) for x in sorted(self._literals, key=len, reverse=True))  # longest first
        self._known = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)
        self._forward: dict[str, str] = {}  # spelling -> placeholder
        self._back: dict[str, str] = {}  # placeholder -> spelling
        self._counts: Counter[str] = Counter()
        self.replaced: Counter[str] = Counter()  # category -> occurrences replaced (for measurement)

    def _placeholder(self, category: str, spelling: str) -> str:
        if spelling not in self._forward:
            self._counts[category] += 1
            n = self._counts[category]
            token = f"[{category}]" if n == 1 else f"[{category}_{n}]"
            self._forward[spelling], self._back[token] = token, spelling
        self.replaced[category] += 1
        return self._forward[spelling]

    def apply(self, text: str) -> str:
        text = self._known.sub(lambda m: self._placeholder(self._literals[m.group(0).lower()], m.group(0)), text)
        return PHONE.sub(lambda m: self._placeholder("PHONE", m.group(0)), text)

    def restore(self, text: str) -> str:
        """Put back what `apply` replaced; a placeholder this redactor did not issue is left as it is."""
        return PLACEHOLDER.sub(lambda m: self._back.get(m.group(0), m.group(0)), text)
