"""Versioned prompts, one YAML file per version (`agent@v1.yaml`, `agent@v2.yaml`, ...). Old versions are kept.

The prompt text is part of every request, hence of every cassette key: editing a committed prompt makes replay fail
until the system is re-recorded. A new behaviour gets a new version.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from priorauth.data.schema import Case
from priorauth.policy.criteria import Procedure, get_procedure

PROMPTS_DIR = Path(__file__).resolve().parent

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Prompt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str
    effort: Effort
    max_tokens: int = Field(gt=0)
    system: str
    task: str  # str.format template, see render_task

    def render_task(self, case: Case) -> str:
        """Fill the template. Placeholders: case_id, procedure_code, procedure_name, policy_id, submitted_on,
        n_pages, criteria (id and title per line), policy (full text of every criterion), pages (whole record)."""
        procedure = get_procedure(case.procedure_code)
        criteria = "\n".join(f"- {c.id}: {c.title}" for c in procedure.criteria)
        policy = "\n\n".join(criterion_text(procedure, c.id) for c in procedure.criteria)
        pages = "\n".join(f'<page number="{p.number}">\n{p.text}</page>' for p in case.pages)
        return self.task.format(
            case_id=case.case_id,
            procedure_code=procedure.code,
            procedure_name=procedure.name,
            policy_id=procedure.policy_id,
            submitted_on=case.submitted_on.strftime("%m/%d/%Y"),
            n_pages=len(case.pages),
            criteria=criteria,
            policy=policy,
            pages=pages,
        ).strip()


def criterion_text(procedure: Procedure, criterion_id: str) -> str:
    """The policy text of one criterion, as the agent's search_policy tool returns it."""
    c = procedure.criterion(criterion_id)
    return (
        f"{c.id} - {c.title} ({procedure.policy_id})\n"
        f"Requirement: {c.requirement}\n"
        f"MET when: {c.met_when}\n"
        f"NOT_MET when: {c.not_met_when}\n"
        f"INSUFFICIENT when: {c.insufficient_when}"
    )


def load_prompt(prompt_id: str) -> Prompt:
    path = PROMPTS_DIR / f"{prompt_id}.yaml"
    if not path.is_file():
        known = sorted(p.stem for p in PROMPTS_DIR.glob("*.yaml"))
        raise KeyError(f"unknown prompt {prompt_id!r}; known: {', '.join(known)}")
    prompt = Prompt.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if prompt.id != prompt_id:
        raise ValueError(f"{path.name} declares id {prompt.id!r}")
    return prompt
