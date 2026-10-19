"""The example cases of the demo's Try page: synthetic dev records, one per trap, with a label saying what to look for.

Each was chosen among the dev cases that agent v2 got entirely right through the API pipeline
(results/pipeline_dev_errors.jsonl does not list them), so the demo shows the system as measured, not a lucky run.
Their decisions come from the real model responses recorded by eval/pipeline.py, replayed.
"""

from dataclasses import dataclass
from pathlib import Path

from priorauth.data.schema import Case, Gold


@dataclass(frozen=True)
class Example:
    id: str
    case_id: str  # in data/dev
    title: str
    label: str  # the trap, in a few words
    look_for: str  # what a visitor should check in the result


EXAMPLES: tuple[Example, ...] = (
    Example(
        "clean-approve-pt",
        "dev-001",
        "Physical therapy extension, complete record",
        "Clean record to approve",
        "Every criterion is MET with a citation; the engine approves.",
    ),
    Example(
        "clean-approve-mri",
        "dev-029",
        "Lumbar MRI, criteria written in other words",
        "Clean record to approve (paraphrased)",
        "The notes never use the policy's wording; the agent still finds and quotes the evidence.",
    ),
    Example(
        "clean-pend",
        "dev-009",
        "Physical therapy extension without measurable progress",
        "Clean record to pend",
        "One criterion is NOT_MET; the engine pends with that reason.",
    ),
    Example(
        "dose-x4",
        "dev-005",
        "Biologic for rheumatoid arthritis, dose four times the label",
        "Trap: dose x4",
        "The requested dose is above the policy maximum; it must not be approved.",
    ),
    Example(
        "laterality",
        "dev-008",
        "Lumbar MRI, side swapped between request and notes",
        "Trap: left/right inverted",
        "The request and the clinical notes disagree on the side.",
    ),
    Example(
        "date-conflict",
        "dev-004",
        "Lumbar MRI, dates contradict the stated length of conservative therapy",
        "Trap: inconsistent dates",
        "The stated duration of conservative therapy and the dates in the notes disagree; the engine pends.",
    ),
    Example(
        "injection",
        "dev-042",
        "Lumbar MRI with an instruction hidden in a note",
        "Trap: hidden instruction (prompt injection)",
        "A note tells the automated reviewer to approve; it is flagged and the case is never approved.",
    ),
    Example(
        "contradiction",
        "dev-002",
        "Lumbar MRI, two pages contradict each other",
        "Trap: contradiction",
        "One page reports a red flag that another denies; the engine pends.",
    ),
    Example(
        "missing-evidence",
        "dev-007",
        "Lumbar MRI with a criterion the record never documents",
        "Trap: missing evidence",
        "No page supports one criterion; it is INSUFFICIENT, not guessed.",
    ),
    Example(
        "biologic-paraphrase",
        "dev-011",
        "Biologic for rheumatoid arthritis, paraphrased record",
        "Clean record to approve (paraphrased)",
        "Disease activity and prior therapy are described in other words; the case is approved with citations.",
    ),
)


def load_examples(data_dir: Path) -> list[tuple[Example, Case, Gold]]:
    out = []
    for example in EXAMPLES:
        case = Case.model_validate_json((data_dir / "dev" / "cases" / f"{example.case_id}.json").read_bytes())
        gold = Gold.model_validate_json((data_dir / "dev" / "gold" / f"{example.case_id}.json").read_bytes())
        out.append((example, case, gold))
    return out
