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
