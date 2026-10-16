# Progress

One section per phase of [`PLAN.md`](../PLAN.md). Each entry says what was done, what was measured (with the
command or file that proves it) and what is left. Nothing here is a claim without a pointer to its proof.

## P0 — Foundation

**Done**
- MIT license, `uv` project (Python 3.12, `src/` layout), locked environment (`uv.lock`).
- `ruff` (lint + format), `mypy --strict` on `src/`, `eval/` and `tests/`, `pytest` + Hypothesis.
- `Makefile` targets `setup`, `lint`, `fmt`, `test`. Targets are added only once they work.
- `pre-commit` with local hooks that run the locked tool versions (`uv run ...`).
- Empty package tree under `src/priorauth/` (`data`, `policy`, `agent`, `llm`, `api`, `jobs`, `security`, `obs`).
- `.github/workflows/ci.yml`: `make setup`, `make lint`, `make test` on every push to `main` and on pull requests.

**Measured**
- `make setup lint test` green locally (1 smoke test).
- CI green on GitHub: workflow `CI`, run #1, commit `4d29e66`.

**Left**
- Nothing for P0.

## P1 — Synthetic data and gold set

**Done**
- `src/priorauth/policy/criteria.py`: three fictional procedures with thresholds as constants —
  lumbar MRI (5 criteria), physical therapy continuation (4), fictional biologic "fictimab" for RA (4).
- `src/priorauth/data/`: seeded generator (one `random.Random` per case), per-procedure builders, noise pages
  (fax cover, eligibility, wellness visit, refill call, scheduling, intake form, X-ray report), nine traps.
- Case files (`data/<split>/cases/`) are separate from gold files (`data/<split>/gold/`); gold holds the
  per-criterion status, the exact quotes, a rationale, the trap and the expected decision.
- `data/test.sha256` freezes the test split: `make data` refuses to write a different test split.
- `make data`, `make data-check`, optional `make data-pdf` (PDFs in `build/`, not committed).
- `data/README.md` documents the layout, label semantics and the one known coupling between criteria.

**Measured** (all enforced by `tests/test_data.py`, `tests/test_criteria.py`, `tests/test_pdf.py`)
- 100 dev cases (65 expected `PEND`), 60 test cases (39 expected `PEND`, plan requires at least 30).
- Trap counts per split equal `SPLIT_COUNTS`; 3 `injection` cases in test, 8 in dev.
- 3 to 8 pages per case; 1,117 gold quotes, each found verbatim on its cited page.
- Committed `data/` is byte-for-byte what the generator produces (`make data-check`).
- Each trap changes only its target criterion (one documented exception, see `data/README.md`).
- No trap or label string appears in case files; NPIs start with 9, phone numbers use 555-01xx.

**Design choices worth defending**
- `INSUFFICIENT` covers both "silent" and "self-contradictory" records: in both cases the right action is to ask a
  human, so `contradiction` and `date_conflict` targets are labelled `INSUFFICIENT`, not `NOT_MET`.
- `injection` cases are otherwise fully approvable. The only correct way to `PEND` them is to notice the tampering.
- Two ambiguities found while reading generated cases were removed rather than labelled: a symptom-duration date
  conflict also contradicted the therapy timeline, and a "missing duration" case still implied the duration through
  six weeks of documented therapy.

**Left**
- Data realism is limited by templates: one author wrote both the templates and (in P2) the keyword baseline,
  so the baseline will be optimistic on standard phrasing. To record in `docs/LIMITS.md` (P3).

## P2 — Policy engine, baseline, evaluation harness

**Done**
- `src/priorauth/policy/engine.py`: `decide(CaseFindings) -> Decision`. `APPROVE` only if every criterion of the
  procedure has a `MET` finding with verified evidence, no conflict, no disagreeing duplicate, no unknown criterion,
  no detected injection. Otherwise `PEND` with typed reasons. `Outcome` has two values; there is no deny path.
- `src/priorauth/agent/verifier.py` (pulled forward from P3 because the engine and the evidence-validity metric need
  it): normalized-substring check of each quote on its cited page, minimum quote length, page bounds; unverified
  `MET`/`NOT_MET` findings become `INSUFFICIENT`; invalid citations are counted before the downgrade.
- `src/priorauth/pipeline.py`: system → verifier → engine; a system exception fails closed to `PEND`.
- `eval/baselines/rules.py`: baseline 1, keyword and regex rules, no LLM. Written and checked on `dev` only, then
  frozen and run once on `test`.
- `eval/metrics.py`: dangerous-approval rate, unjustified-`PEND` rate, decision and per-criterion accuracy with a
  confusion matrix, evidence validity, injection detection and false flags, cost and latency mean/p50/p95,
  per-trap and per-procedure breakdowns, 95 % Wilson intervals.
- `eval/run.py` + `make eval-offline`: `results/<system>_<split>.json` and `results/<system>_<split>_errors.jsonl`.

**Measured**
- Tests: every one of the 8^n per-criterion report combinations for each procedure (with and without injection),
  a Hypothesis property over arbitrary findings (never anything but `APPROVE`/`PEND`, and `APPROVE` only when
  earned), and the engine applied to the 160 gold label sets reproduces every expected decision
  (`tests/test_engine.py`). Metrics checked on hand-drawn cases (`tests/test_metrics.py`).
- `results/` is regenerated byte for byte by `tests/test_eval_run.py`, locally and in CI (run #5, `509e106`).
- Baseline 1 (`results/rules_baseline_dev.json`, `results/rules_baseline_test.json`):

  | split | dangerous approvals | unjustified PEND | decision accuracy | criterion accuracy | evidence validity |
  |---|---|---|---|---|---|
  | dev | 8/65 (95 % CI 0.064–0.225) | 15/35 | 77/100 | 388/433 | 526/526 |
  | test | 5/39 (95 % CI 0.056–0.267) | 9/21 | 46/60 | 230/258 | 311/311 |

  Every dangerous approval is a `date_conflict` case (no date arithmetic); every unjustified `PEND` is a
  `paraphrase` case (no understanding of reworded facts). It catches all other traps — expected, since the same
  author wrote the templates and the rules (see P1 "Left"). Latency is `null` for this system (no model call).

**Left**
- P3: LLM client with cassettes, agent loop and tools, baseline 2 (single LLM call), prompts v1/v2 iterated on `dev`,
  one run on `test`, `eval/gate.py` in CI, `docs/LIMITS.md` from the `*_errors.jsonl` files.
- The verifier's "invalid citations before verification" rate is computed, but is only informative once an LLM
  produces the citations (baseline 1 quotes the page verbatim, so it is 100 % by construction).
