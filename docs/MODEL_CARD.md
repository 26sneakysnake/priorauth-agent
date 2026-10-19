# Model card: agent v2 for prior-authorization evidence extraction

Synthetic data only. No claim of clinical validity, of compliance or of certification. Numbers are not written
here by hand; each one lives in the result file named next to it, and the generated `README.md` tabulates them.

## System

- **What it is.** A tool-using agent: model `claude-haiku-5-5` (production configuration) or `claude-sonnet-5-5`
  (comparison), with prompt `agent@v2` (`src/priorauth/agent/prompts/agent@v2.yaml`), five tools and a manual
  loop (`src/priorauth/agent/`).
- **What it outputs.** Findings, not decisions: per policy criterion a status, evidence quotes with page numbers,
  and a conflict flag; plus a prompt-injection flag.
- **Who decides.** The deterministic verifier and policy engine decide APPROVE or PEND (`docs/DESIGN.md`).
- **In production configuration.** The API wraps the agent in the model view: minimum-necessary pages, redacted
  identifiers (`src/priorauth/api/app.py`, `default_system`).
- **Determinism.** Sampling cannot be fixed (these models reject `temperature=0`), so reproducibility comes from
  recorded responses (`eval/cassettes/`), not from the model.

## Intended use

- **Inside this project.** Demonstrating, on synthetic records, how an LLM can prepare a prior-authorization review:
  find and quote the evidence for each criterion of three fictional payer policies (lumbar MRI, physical therapy
  extension, a fictional biologic for rheumatoid arthritis, `src/priorauth/policy/policies.yaml`).
- **Who uses the output.** A human reviewer, through the review UI. An APPROVE can only come from the engine.

**Out of scope:**
- Real patients or real records.
- Denying care (the code cannot).
- Clinical advice.
- Any policy other than the three fictional ones.
- Unattended production use.

## Data

- **Evaluation data.** Synthetic records generated from templates with a fixed seed (`src/priorauth/data/`):
  - **dev**, used to iterate on prompts;
  - **test**, frozen;
  - **robust**, new templates and OCR noise, written after the prompts were frozen.
- **Labels.** Each case has gold labels per criterion and an expected decision.
- **Traps.** Dose four times the label, left/right swapped, dates that contradict a stated duration, contradictory
  pages, missing evidence, paraphrased wording, instructions hidden in a note.
- **Training.** No training or fine-tuning was done.

## Evaluation

Metrics (`eval/metrics.py`), each with a 95 % Wilson interval:
- **Dangerous-approval rate** (expected PEND, approved): the central metric.
- **Unjustified-PEND rate.**
- **Decision accuracy.**
- **Criterion accuracy.**
- **Citation validity before verification.**
- **Injection detection.**
- **Cost and latency.**

Where to find the numbers:
- **Agent v2 on whole records:** `results/agent_v2_haiku_<split>.json` and `results/agent_v2_sonnet_<split>.json`.
- **Agent v2 through the API pipeline:** `results/pipeline_<split>.json`.
- **All systems, including the baselines:** `results/comparison.md`.
- **Whole records vs pipeline:** `results/pipeline_comparison.md`.

## Known failure modes

From the `*_errors.jsonl` files (details in `docs/LIMITS.md`):
- **Side mismatch.** A left/right mismatch is recorded as NOT_MET *with* a conflict flag; the engine keeps
  INSUFFICIENT. The decision is still PEND.
- **Wrong quote source.** The model quotes text that is not on the page: a page header from another page, or the
  page summary returned by `list_pages`. The verifier rejects it and the case is pended without need.
- **Verification is literal.** The verifier checks words, not meaning: a real quote that does not support the
  claimed status passes.

## Caveats

- **Same author.** One author wrote the data, the labels, the baselines and the prompts.
- **Small and synthetic.** The samples are small and synthetic; nothing here predicts performance on real faxes.
- **Single runs.** Each system was recorded once, so differences within the intervals are not established.
- **Hidden instructions.** Detection depends on the model; when the model does flag one, the engine never approves
  the case.
