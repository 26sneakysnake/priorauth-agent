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
