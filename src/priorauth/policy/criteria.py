"""Payer policy criteria for three fictional procedures, loaded from `policies.yaml` next to this module.

The payer ("Example Health Plan"), the policy numbers and the biologic "fictimab" are fictional. The criteria are
loosely modelled on the shape of real utilization-management policies, but they are not clinical guidance and are not
copied from any real payer. Numeric thresholds are parameters of the policy file: its text refers to them by name and
this module exposes them as constants, so that the policy text, the data generator and the baselines can never
disagree about a number.
"""

import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr, ValidationError

POLICY_FILE = Path(__file__).with_name("policies.yaml")

Parameter = StrictInt | StrictFloat | StrictStr


class PolicyError(ValueError):
    """A policy file that cannot be loaded; the message lists every problem found."""


class Criterion(BaseModel):
    """One policy criterion. `requirement` is the policy text an agent is allowed to read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    requirement: str
    met_when: str
    not_met_when: str
    insufficient_when: str


class Procedure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    name: str
    policy_id: str
    criteria: tuple[Criterion, ...]

    def criterion(self, criterion_id: str) -> Criterion:
        for c in self.criteria:
            if c.id == criterion_id:
                return c
        raise KeyError(criterion_id)

    @property
    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.criteria)


class PolicySet(BaseModel):
    """A loaded policy file: placeholders filled in, every rule of the file checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payer: str
    parameters: dict[str, Parameter]
    procedures: dict[str, Procedure]


class _PolicyFile(BaseModel):
    """The file as written: texts still hold their {NAME} placeholders."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payer: StrictStr
    parameters: dict[str, Parameter]
    fragments: dict[str, StrictStr] = {}
    procedures: tuple[Procedure, ...]


_TEXT_FIELDS = ("title", "requirement", "met_when", "not_met_when", "insufficient_when")


def _placeholders(text: str, where: str, problems: list[str]) -> set[str]:
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
        return names
    for _, name, spec, conversion in parsed:
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            problems.append(f"{where}: placeholder {{{name}}} must be a bare name")
        names.add(name)
    return names


def parse_policies(raw: Any, source: str = "policy file") -> PolicySet:
    """Validate a parsed policy document and fill in its placeholders; raise PolicyError listing every problem."""
    try:
        spec = _PolicyFile.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"{source}: {exc}") from None
    problems: list[str] = []
    values: dict[str, Parameter] = {**spec.parameters, **spec.fragments}
    problems += [f"{n} is both a parameter and a fragment" for n in sorted(set(spec.parameters) & set(spec.fragments))]
    problems += [f"{n}: names are UPPER_SNAKE_CASE" for n in sorted(values) if not (n.isidentifier() and n.isupper())]
    for name, fragment in spec.fragments.items():
        if _placeholders(fragment, f"fragment {name}", problems):
            problems.append(f"fragment {name}: fragments cannot contain placeholders")

    used: set[str] = set()
    codes: set[str] = set()
    for proc in spec.procedures:
        if proc.code in codes:
            problems.append(f"{proc.code}: duplicate procedure code")
        codes.add(proc.code)
        if not proc.criteria:
            problems.append(f"{proc.code}: a procedure needs at least one criterion")
        if len(set(proc.criterion_ids)) != len(proc.criterion_ids):
            problems.append(f"{proc.code}: duplicate criterion id")
        used |= _placeholders(proc.name, f"{proc.code}.name", problems)
        used |= _placeholders(proc.policy_id, f"{proc.code}.policy_id", problems)
        for c in proc.criteria:
            if not c.id.startswith(f"{proc.code}."):
                problems.append(f"{c.id}: criterion ids start with the procedure code ({proc.code}.)")
            for field in _TEXT_FIELDS:
                used |= _placeholders(getattr(c, field), f"{c.id}.{field}", problems)
    problems += [f"unknown placeholder {{{n}}}" for n in sorted(used - set(values))]
    problems += [f"{n} is never used" for n in sorted(set(values) - used)]
    if problems:
        raise PolicyError(f"{source}: " + "; ".join(problems))
    procedures = {proc.code: _fill(proc, values) for proc in spec.procedures}
    return PolicySet(payer=spec.payer, parameters=spec.parameters, procedures=procedures)


def _fill(proc: Procedure, values: dict[str, Parameter]) -> Procedure:
    criteria = tuple(
        c.model_copy(update={f: getattr(c, f).format_map(values) for f in _TEXT_FIELDS}) for c in proc.criteria
    )
    update = {"name": proc.name.format_map(values), "policy_id": proc.policy_id.format_map(values)}
    return proc.model_copy(update={**update, "criteria": criteria})


def load_policies(path: Path = POLICY_FILE) -> PolicySet:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path.name}: {exc}") from None
    return parse_policies(raw, source=path.name)


def _parameter[T: (int, float, str)](name: str, kind: type[T]) -> T:
    value = POLICIES.parameters.get(name)
    if not isinstance(value, kind) or isinstance(value, bool):
        raise PolicyError(f"{POLICY_FILE.name}: parameter {name} must be a {kind.__name__}")
    return value


POLICIES = load_policies()
PAYER_NAME = POLICIES.payer
PROCEDURES: dict[str, Procedure] = dict(POLICIES.procedures)
MRI_LUMBAR = PROCEDURES["MRI_LUMBAR"]
PT_EXTENSION = PROCEDURES["PT_EXTENSION"]
BIOLOGIC_RA = PROCEDURES["BIOLOGIC_RA"]

# Lumbar spine MRI
MIN_SYMPTOM_WEEKS = _parameter("MIN_SYMPTOM_WEEKS", int)
MIN_CONSERVATIVE_WEEKS = _parameter("MIN_CONSERVATIVE_WEEKS", int)

# Physical therapy continuation
MAX_PLAN_WEEKS = _parameter("MAX_PLAN_WEEKS", int)

# Biologic therapy for rheumatoid arthritis (fictional drug)
DRUG_NAME = _parameter("DRUG_NAME", str)
MIN_CDAI = _parameter("MIN_CDAI", int)
MIN_DAS28 = _parameter("MIN_DAS28", float)
MIN_MTX_WEEKS = _parameter("MIN_MTX_WEEKS", int)
MIN_MTX_MG_WEEKLY = _parameter("MIN_MTX_MG_WEEKLY", int)
STANDARD_DOSE_MG = _parameter("STANDARD_DOSE_MG", int)
DOSE_INTERVAL_DAYS = _parameter("DOSE_INTERVAL_DAYS", int)
MAX_MG_PER_INTERVAL = _parameter("MAX_MG_PER_INTERVAL", int)
SCREENING_MAX_AGE_DAYS = _parameter("SCREENING_MAX_AGE_DAYS", int)


def get_procedure(code: str) -> Procedure:
    try:
        return PROCEDURES[code]
    except KeyError:
        raise KeyError(f"unknown procedure code: {code!r}") from None
