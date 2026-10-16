"""Payer policy criteria for three fictional procedures.

The payer ("Example Health Plan"), the policy numbers and the biologic "fictimab" are fictional. The criteria are
loosely modelled on the shape of real utilization-management policies, but they are not clinical guidance and are not
copied from any real payer. Numeric thresholds are module constants so that the policy text, the data generator and
the baselines can never disagree about a number.
"""

from pydantic import BaseModel, ConfigDict

PAYER_NAME = "Example Health Plan (fictional)"

# Lumbar spine MRI
MIN_SYMPTOM_WEEKS = 6
MIN_CONSERVATIVE_WEEKS = 6

# Physical therapy continuation
MAX_PLAN_WEEKS = 12

# Biologic therapy for rheumatoid arthritis (fictional drug)
DRUG_NAME = "fictimab"
MIN_CDAI = 10
MIN_DAS28 = 3.2
MIN_MTX_WEEKS = 12
MIN_MTX_MG_WEEKLY = 15
STANDARD_DOSE_MG = 40
DOSE_INTERVAL_DAYS = 14
MAX_MG_PER_INTERVAL = 80
SCREENING_MAX_AGE_DAYS = 180


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


_CONFLICT = "the record contradicts itself on this point (two pages disagree, or dates disagree with a stated duration)"

MRI_LUMBAR = Procedure(
    code="MRI_LUMBAR",
    name="MRI lumbar spine without contrast",
    policy_id="EHP-RAD-014 (fictional)",
    criteria=(
        Criterion(
            id="MRI_LUMBAR.symptom_duration",
            title="Symptom duration",
            requirement=(
                f"Low back pain, with or without radicular symptoms, has been present for at least "
                f"{MIN_SYMPTOM_WEEKS} weeks before the request."
            ),
            met_when=f"a note documents symptoms lasting {MIN_SYMPTOM_WEEKS} weeks or more",
            not_met_when=f"the documented duration is shorter than {MIN_SYMPTOM_WEEKS} weeks",
            insufficient_when=f"no duration or onset is documented, or {_CONFLICT}",
        ),
        Criterion(
            id="MRI_LUMBAR.conservative_therapy",
            title="Conservative therapy tried",
            requirement=(
                f"At least {MIN_CONSERVATIVE_WEEKS} weeks of conservative therapy (physical therapy, "
                "supervised exercise and/or anti-inflammatory medication) were tried without adequate relief."
            ),
            met_when=f"a note documents {MIN_CONSERVATIVE_WEEKS}+ weeks of conservative therapy that failed",
            not_met_when=(f"no conservative therapy was tried, or it lasted less than {MIN_CONSERVATIVE_WEEKS} weeks"),
            insufficient_when=f"conservative therapy is not mentioned, or {_CONFLICT}",
        ),
        Criterion(
            id="MRI_LUMBAR.red_flags",
            title="Red-flag screen",
            requirement=(
                "A red-flag screen is documented (bowel or bladder dysfunction, saddle anesthesia, fever, "
                "unexplained weight loss, history of cancer) and no page reports a red flag. Cases with red "
                "flags follow the urgent imaging pathway, which is outside this policy."
            ),
            met_when="a negative red-flag screen is documented and no page reports a red flag",
            not_met_when="a red flag is documented and the screen is not contradicted elsewhere",
            insufficient_when=f"no red-flag screen is documented, or {_CONFLICT}",
        ),
        Criterion(
            id="MRI_LUMBAR.neuro_exam",
            title="Neurological exam",
            requirement="A neurological exam (motor strength, reflexes and sensation) is documented.",
            met_when="a note documents motor, reflex and sensory findings",
            not_met_when="the note states the neurological exam was not performed",
            insufficient_when="no neurological exam is documented",
        ),
        Criterion(
            id="MRI_LUMBAR.laterality",
            title="Side matches the request",
            requirement=(
                "The side of the radicular symptoms in the clinical notes matches the side stated in the "
                "clinical indication of the request."
            ),
            met_when="the request and the clinical notes name the same side",
            not_met_when="the request names one side and the clinical notes the other",
            insufficient_when="the side is not documented in the request or in the notes",
        ),
    ),
)

PT_EXTENSION = Procedure(
    code="PT_EXTENSION",
    name="Outpatient physical therapy, additional visits",
    policy_id="EHP-REH-022 (fictional)",
    criteria=(
        Criterion(
            id="PT_EXTENSION.diagnosis",
            title="Qualifying diagnosis",
            requirement=(
                "A qualifying musculoskeletal diagnosis is documented with its ICD-10-CM code, on the request "
                "and in the clinical notes. Agreement on the body side is assessed by the laterality criterion."
            ),
            met_when="the diagnosis and its ICD-10-CM code are documented",
            not_met_when="the documented diagnosis is not musculoskeletal",
            insufficient_when="the diagnosis or its code is missing",
        ),
        Criterion(
            id="PT_EXTENSION.plan_of_care",
            title="Plan of care",
            requirement=(
                "A plan of care states visit frequency and total duration; the total duration does not exceed "
                f"{MAX_PLAN_WEEKS} weeks and the plan dates are consistent with the stated duration."
            ),
            met_when=f"frequency and duration are stated, duration <= {MAX_PLAN_WEEKS} weeks, dates consistent",
            not_met_when=f"the planned duration exceeds {MAX_PLAN_WEEKS} weeks",
            insufficient_when=f"no plan of care is documented, or {_CONFLICT}",
        ),
        Criterion(
            id="PT_EXTENSION.progress",
            title="Documented progress",
            requirement=(
                "Objective, measurable functional progress since the start of care is documented "
                "(range of motion, strength, a validated functional score or a functional milestone)."
            ),
            met_when="a note documents measurable improvement since the start of care",
            not_met_when="the notes document no measurable improvement",
            insufficient_when=f"progress is not documented, or {_CONFLICT}",
        ),
        Criterion(
            id="PT_EXTENSION.laterality",
            title="Side matches the request",
            requirement="The body side treated in the therapy notes matches the side on the request.",
            met_when="the request and the therapy notes name the same side",
            not_met_when="the request names one side and the therapy notes the other",
            insufficient_when="the side is not documented in the request or in the notes",
        ),
    ),
)

BIOLOGIC_RA = Procedure(
    code="BIOLOGIC_RA",
    name=f"{DRUG_NAME} (fictional anti-TNF biologic) for rheumatoid arthritis",
    policy_id="EHP-RX-031 (fictional)",
    criteria=(
        Criterion(
            id="BIOLOGIC_RA.diagnosis",
            title="Diagnosis and disease activity",
            requirement=(
                "Rheumatoid arthritis is documented by a rheumatologist with at least moderate disease activity "
                f"(CDAI > {MIN_CDAI} or DAS28 > {MIN_DAS28})."
            ),
            met_when=f"RA is documented with CDAI > {MIN_CDAI} or DAS28 > {MIN_DAS28}",
            not_met_when="RA activity is documented as low or in remission",
            insufficient_when="no disease activity measure is documented",
        ),
        Criterion(
            id="BIOLOGIC_RA.first_line_failure",
            title="First-line therapy failure",
            requirement=(
                f"Inadequate response to at least {MIN_MTX_WEEKS} weeks of methotrexate at "
                f"{MIN_MTX_MG_WEEKLY} mg weekly or more, or documented intolerance of methotrexate."
            ),
            met_when=f"{MIN_MTX_WEEKS}+ weeks of methotrexate >= {MIN_MTX_MG_WEEKLY} mg/week failed, or intolerance",
            not_met_when=f"methotrexate was taken for less than {MIN_MTX_WEEKS} weeks, or not tried",
            insufficient_when=f"methotrexate history is not documented, or {_CONFLICT}",
        ),
        Criterion(
            id="BIOLOGIC_RA.dose",
            title="Requested dose within range",
            requirement=(
                f"The requested {DRUG_NAME} dose is {STANDARD_DOSE_MG} mg subcutaneously every "
                f"{DOSE_INTERVAL_DAYS} days; escalation is allowed up to {MAX_MG_PER_INTERVAL} mg per "
                f"{DOSE_INTERVAL_DAYS} days."
            ),
            met_when=f"the requested dose is <= {MAX_MG_PER_INTERVAL} mg per {DOSE_INTERVAL_DAYS} days",
            not_met_when=f"the requested dose exceeds {MAX_MG_PER_INTERVAL} mg per {DOSE_INTERVAL_DAYS} days",
            insufficient_when="the requested dose or interval is missing",
        ),
        Criterion(
            id="BIOLOGIC_RA.pretreatment_screening",
            title="Pre-treatment screening",
            requirement=(
                "Tuberculosis screening (interferon-gamma release assay) and hepatitis B serology are "
                f"documented as negative within {SCREENING_MAX_AGE_DAYS} days before the request."
            ),
            met_when="both screens are documented negative within the window",
            not_met_when="a screen is positive and no page says otherwise",
            insufficient_when=f"a screen is missing, or {_CONFLICT}",
        ),
    ),
)

PROCEDURES: dict[str, Procedure] = {p.code: p for p in (MRI_LUMBAR, PT_EXTENSION, BIOLOGIC_RA)}


def get_procedure(code: str) -> Procedure:
    try:
        return PROCEDURES[code]
    except KeyError:
        raise KeyError(f"unknown procedure code: {code!r}") from None
