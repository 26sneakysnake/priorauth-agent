"""Building blocks for synthetic record pages: per-case context, documents and rendering helpers."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from priorauth.data.schema import Patient
from priorauth.policy.models import Status

SYNTHETIC_FOOTER = "SYNTHETIC RECORD - generated for software testing, not a real patient"


def fmt(d: date) -> str:
    """US clinical date format."""
    return d.strftime("%m/%d/%Y")


def weeks_before(d: date, weeks: int, jitter_days: int = 0) -> date:
    return d - timedelta(weeks=weeks, days=jitter_days)


_UNITS = (
    *("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
    *("eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"),
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_word(n: int) -> str:
    """Spell out 0..99 the way a clinician might dictate it ("twenty-two")."""
    if not 0 <= n <= 99:
        raise ValueError(n)
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    return _TENS[tens] if unit == 0 else f"{_TENS[tens]}-{_UNITS[unit]}"


@dataclass(frozen=True)
class Pronouns:
    subj: str
    obj: str
    poss: str

    @property
    def Subj(self) -> str:  # sentence-initial form
        return self.subj.capitalize()

    @property
    def Poss(self) -> str:
        return self.poss.capitalize()


FEMALE = Pronouns("she", "her", "her")
MALE = Pronouns("he", "him", "his")


@dataclass
class Ctx:
    """Everything a builder needs about one case. All randomness goes through `rng`."""

    rng: Random
    case_id: str
    patient: Patient
    pron: Pronouns
    age: int
    submitted_on: date
    visit_date: date

    @property
    def person(self) -> str:
        return "woman" if self.patient.sex == "female" else "man"

    def pick[T](self, options: tuple[T, ...] | list[T]) -> T:
        return self.rng.choice(options)

    def clinician(self, first: tuple[str, ...], last: tuple[str, ...]) -> str:
        return f"{self.rng.choice(first)} {self.rng.choice(last)}"

    def npi(self) -> str:
        # Real NPIs start with 1 or 2; a leading 9 cannot collide with an issued number.
        return "9" + "".join(str(self.rng.randrange(10)) for _ in range(9))


@dataclass
class Section:
    heading: str | None
    lines: list[str] = field(default_factory=list)
    paragraph: bool = False

    def render(self) -> str:
        body = " ".join(self.lines) if self.paragraph else "\n".join(self.lines)
        return f"{self.heading}:\n{body}" if self.heading else body


@dataclass
class Doc:
    """One document of the record; each document is rendered as exactly one page."""

    key: str
    kind: str
    header: list[str]
    sections: list[Section] = field(default_factory=list)

    def section(self, heading: str | None, *, paragraph: bool = False) -> Section:
        for s in self.sections:
            if s.heading == heading:
                return s
        s = Section(heading, paragraph=paragraph)
        self.sections.append(s)
        return s

    def add(self, heading: str | None, line: str, *, paragraph: bool = False) -> str:
        self.section(heading, paragraph=paragraph).lines.append(line)
        return line

    def render(self, page: int, total: int) -> str:
        parts = ["\n".join(self.header), *(s.render() for s in self.sections if s.lines)]
        parts.append(f"Page {page} of {total} | {SYNTHETIC_FOOTER}")
        return "\n\n".join(parts) + "\n"


@dataclass
class LabelDraft:
    """Gold label for one criterion; evidence is (doc key, exact quote) until pages are numbered."""

    status: Status
    evidence: list[tuple[str, str]]
    rationale: str


@dataclass
class Draft:
    """What a procedure builder returns: core documents (request form first) and gold labels."""

    docs: list[Doc]
    labels: dict[str, LabelDraft]
    paraphrased: list[str] = field(default_factory=list)
    sender: str = ""  # clinic named on the fax cover

    def doc(self, key: str) -> Doc:
        for d in self.docs:
            if d.key == key:
                return d
        raise KeyError(key)
