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

## P3 — Agent loop, verifier, comparison, gate

**Done**
- `src/priorauth/llm/`: `pricing.py` (published prices of claude-haiku-5-5 and claude-sonnet-5-5, read 2026-10-19),
  `cassette.py` (one JSON line per call, keyed by the sha256 of the canonical request; no request, no header
  stored), `client.py` (`LLM_MODE=live|record|replay`, default replay; key and base URL passed explicitly from
  `PRIORAUTH_ANTHROPIC_API_KEY` / `PRIORAUTH_ANTHROPIC_BASE_URL`, never from `ANTHROPIC_*`; ledger
  `results/spend.json` written after every real call; phase cap 10 USD, run cap, per-case cap checked before a
  call is sent). No temperature is sent: both models reject non-default sampling values.
- `src/priorauth/agent/`: `tools.py` (`list_pages`, `read_page`, `search_policy`, `record_finding`, `finish`,
  strict Pydantic validation, errors returned to the model), `loop.py` (manual tool-use loop, `max_steps=12`, stops
  and pends on two idle steps, the step limit, a refusal or a cap; append-only; system + tools cached).
  Prompts in `prompts/`: `agent@v1`, `agent@v2` (written from the v1 errors on dev only), `single@v1` (baseline 2),
  `agent@lenient` (probe) and `agent@degraded` (gate fixture).
- Baseline 2, `eval/baselines/llm_single.py`: one Haiku call, whole record and policy in the prompt,
  schema-constrained JSON output, same verifier and engine.
- `data/robust/`: 40 cases, separate seed and templates, frozen by `data/robust.sha256`, written after agent@v2 and
  baseline 1 were frozen (`src/priorauth/data/robust.py`, `tests/test_robust.py`).
- `eval/`: `run.py` (robust split, cost estimate before a paid run via `budget.py`, nothing written when a cap
  stops a run), `gate.py` + `results/baseline_gate.json` + `make gate`, `report.py` → `results/comparison.md`.
- CI (`.github/workflows/ci.yml`): replay with `LLM_MODE=replay` and no key, `make gate`, gitleaks 8.28.0 over every
  commit.
- `.github/workflows/eval-live.yml` is **written but has never run** (not on GitHub, not locally): it is unvalidated.
  As written, it is manual only, should refuse to start without the `ANTHROPIC_API_KEY` secret and caps a run at
  2 USD on top of the phase cap; none of this has been observed working. Every number above comes from local
  record runs, then replay.
- `docs/LIMITS.md`.

**Measured** (full table with Wilson intervals, cost and latency: [`results/comparison.md`](../results/comparison.md),
generated from `results/*.json` and checked by `tests/test_eval_run.py`)
- Acceptance: `make eval-offline` replays every result without a key and `tests/test_eval_run.py` regenerates
  `results/` byte for byte; `make gate` passes (CI run #9, `8990d08`); `tests/test_gate.py` shows the gate failing
  on `agent@degraded` (replayed); no case with an injection is approved in any committed result
  (`tests/test_eval_run.py::test_no_case_with_a_prompt_injection_is_approved`); gitleaks finds nothing in the
  history or the tree (CI job `secrets`, `make secrets`).
- Agent v2 (Haiku), the gated configuration: 0 dangerous approvals on dev, test and robust; decisions 100/100,
  59/60, 40/40 (`results/agent_v2_haiku_*.json`). Agent v2 (Sonnet): 100/100, 60/60, 40/40 at about 15 times the
  cost per case (`results/agent_v2_sonnet_*.json`).
- Like-for-like (first prompts, written before any run): baseline 2 and agent v1 are close on every split; agent
  v2's gain over v1 comes from one round of error analysis on dev (`results/comparison.md`, `docs/LIMITS.md` §4).
- Baseline 1 collapses on robust (pends every approvable case, flags no injection): the frozen rules only know
  the dev/test templates (`results/rules_baseline_robust.json`).
- Prompt sensitivity: the lenient prompt does not degrade Haiku on dev; the degraded prompt (stale policy
  thresholds) gives 10/65 dangerous approvals with valid citations, which the gate catches
  (`results/agent_lenient_haiku_dev.json`, `results/agent_degraded_haiku_dev.json`).
- Spend: 6.2210 USD of the 10 USD phase cap, every real call included except the first key check
  (`results/spend.json`, `docs/LIMITS.md` §5).

**Left**
- `eval-live.yml` has never run, so it is not validated (it needs the repository secret `ANTHROPIC_API_KEY`).
- Side mismatches recorded as NOT_MET plus conflict (criterion status INSUFFICIENT, decision still PEND): seen on
  dev only after test and robust were run, so no v3 prompt was written (`docs/LIMITS.md` §3).
- Citation checks prove a quote exists, not that it supports the status (`docs/LIMITS.md` §4).

## P4 — API, job queue, FHIR

**Done**
- `src/priorauth/api/fhir.py`: inbound Bundle (Patient, ServiceRequest or MedicationRequest, Condition,
  DocumentReference with one `text/plain` attachment per page) → `Case`. Structure validated with the **R4B** models
  of `fhir.resources` 8.3, then semantic rules (type `collection`, synthetic tag required, one patient with MRN and
  member id, one coded request, readable text pages); every problem is returned as a 422 entry with its location.
  Outbound: a simplified ClaimResponse *inspired by* Da Vinci PAS (APPROVE → X12 action A1, PEND → A4, outcome
  `queued`); there is no denial. **R4B, not R4**: the R4 models exist only in the pydantic-v1 releases of
  fhir.resources; only elements identical in R4 and R4B are used. Nothing is certified or PAS-conformant.
- `src/priorauth/jobs/`: tables `cases`, `jobs`, `webhook_deliveries` (SQLAlchemy 2.1; SQLite/aiosqlite in tests,
  Postgres/asyncpg otherwise). Claim with `FOR UPDATE SKIP LOCKED` on Postgres plus a version-guarded update
  (portable to SQLite), lease renewed by a heartbeat, reclaim after the lease expires, results written only by the
  lease holder, fail-closed PEND after `max_attempts`. Worker pool on `asyncio.Semaphore(N)`. Webhooks: outbox row
  written with the result, `X-PriorAuth-Signature: t=..,v1=HMAC-SHA256(t.body)`, `verify()` with replay tolerance,
  retries with exponential backoff and jitter, payload = identifiers and outcome only.
- `src/priorauth/api/app.py`: `POST /cases` (Idempotency-Key required; 202 + job id; same key and body → same job;
  other body → 409), `GET /cases/{id}`, `GET /cases/{id}/result` (409 until done). `docs/openapi.json` exported by
  `make openapi` and checked by a test. `make run` starts the API and its workers.
- `src/priorauth/llm/fake.py`: a scripted oracle that plays the agent's tool protocol from gold labels (it is not a
  model), behind an LLMClient whose ledger stays in memory.

**Measured** (tests; no API call was made in P4, `results/spend.json` unchanged)
- All 200 synthetic cases round-trip Case → Bundle → Case exactly; invalid bundles give explicit 422s
  (`tests/test_fhir.py`, `tests/test_api.py`).
- End to end with the scripted fake model, one case per trap: Bundle → 202 → worker → ClaimResponse valid for the
  R4B model with the expected outcome → webhook received, signature verified, no record text in the payload
  (`tests/test_api.py::test_end_to_end_bundle_to_claim_response_and_signed_webhook`).
- Idempotency, including five concurrent requests with one key (`tests/test_api.py`, `tests/test_jobs.py`).
- Crash: a worker killed mid-job, its job reclaimed by a new process after the lease (attempts = 2); a worker that
  lost its lease cannot overwrite the result; three crashes → failed, PEND (`tests/test_jobs.py`, `tests/test_api.py`).
- 30 jobs claimed by 40 concurrent claims: each exactly once, on SQLite and on Postgres 16 (locally, and in CI with
  a `postgres:16` service; `tests/test_jobs.py`). CI run #11 (`10214de`): 198 tests passed, the queue tests on both
  backends.

**Left**
- No authentication yet (P6): every route is open.
- In replay mode a new case misses the cassettes and is pended (`CassetteMiss`, fail-closed); real decisions on
  new cases need `LLM_MODE=live` or `record` and a key.
- Schema created with `create_all` (no migrations); one global webhook URL; one webhook sender per deployment is
  assumed (deliveries are leased while in flight, receivers should still drop duplicates by delivery id).

## P5 — Reliability and observability

**Done**
- Tracing (`src/priorauth/obs/tracing.py`): OpenTelemetry SDK, OTLP/HTTP export when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set. One `job.process` span per job (job and case UUIDs, attempt, outcome,
  error kind, LLM calls, tokens, cost); one `llm.call` child per model call (`tokens_in`, `tokens_out`, `cost_usd`,
  `step`, model, replayed, latency, stop reason); one `tool.call` child per tool call (tool name, step, error,
  progress). Attributes pass an allowlist; exceptions are recorded by type only. FastAPI 0.146 also emits its own
  HTTP spans (method, route, status, path): kept, and covered by the leak test.
- Logs (`src/priorauth/obs/logs.py`): JSON, allowlisted fields only, exception type without message. The database
  drivers' loggers are pinned to WARNING and SQLAlchemy hides SQL parameters (`src/priorauth/jobs/db.py`).
- Cost: cost and LLM calls stored per job, `GET /costs/daily`, per-case cap (`PRIORAUTH_CASE_COST_CAP_USD`, from P3)
  and a daily cap (`PRIORAUTH_DAILY_COST_CAP_USD`): workers stop claiming, queued jobs wait, nothing is decided.
  The cap is checked before a claim, so the overshoot is bounded by the jobs in flight times the per-case cap.
- Resilience (`src/priorauth/llm/resilience.py`): retries on 408/409/429/5xx/529, connection errors and timeouts with
  exponential backoff, full jitter and `retry-after`; per-attempt timeout; circuit breaker (closed, open,
  half-open). SDK retries are off. A transient failure (`RetriesExhausted`, `CircuitOpen`) requeues the job with a
  delay instead of pending it, up to `max_attempts`.
- `docker-compose.yml`: Postgres 16 and Jaeger 2.11 (`make up`). `make load-test` (`eval/load_test.py`).

**Measured**
- No record content in logs or spans: `tests/test_observability.py::test_no_record_content_in_logs_or_spans`
  (root logger at DEBUG, a full API run with webhooks, a 422 with record text in a field, malformed JSON with record
  text, a system whose exception message quotes the record; scans messages, every record attribute, the JSON
  output, span names, attributes, events, status and resource). **It failed on its first run**: aiosqlite logged
  every SQL statement with its parameters at DEBUG; fixed by pinning the driver loggers.
- Spans reach a real Jaeger over OTLP and are read back from its query API (`tests/test_jaeger.py`; locally with
  `docker compose up jaeger`, in CI with a `jaegertracing/jaeger:2.11.0` service). CI run #13 (`e98ac34`) green
  with the Postgres and Jaeger services; the Jaeger test cannot skip when `PRIORAUTH_TEST_JAEGER_URL` is set.
- Retries, jitter, `retry-after`, timeouts, breaker, transient requeue and the daily cap: `tests/test_resilience.py`.
- Load test, **simulated model** (scripted oracle, 0.25 s + up to 0.25 s per call, seed 0), 200 cases at once, 32
  workers, in-process API on SQLite: every case done and matching its expected decision; wall time, throughput,
  submission and end-to-end latency in `results/load_test.json` with the machine used. The deterministic part is
  checked by `tests/test_load_test.py`; timings depend on the machine.
- No API call in P5 (`results/spend.json` unchanged).

**Left**
- `docker compose up` was run here for Jaeger only: pulling `postgres:16` hit Docker Hub's rate limit (429), so the
  compose Postgres service is unverified in this environment (Postgres itself is tested locally and in CI).
- The load test measures the API, queue and SQLite under concurrency, not a model; on Postgres or with real model
  latency the numbers would differ. Not run against Postgres.
- One process-wide circuit breaker per transport; no per-model rate limiter; no metrics endpoint (traces only).

## P6 — Security aligned with the HIPAA Security Rule (no compliance claimed)

Aligned with the principles of the HIPAA Security Rule, synthetic data, no claim of compliance or certification, no
BAA. The control matrix is [`docs/HIPAA_ALIGNMENT.md`](HIPAA_ALIGNMENT.md): each covered row names its code and
tests (`tests/test_hipaa_alignment.py` checks they exist, `make hipaa-tests` runs them); the others say not covered.

**Done**
- Model view (`src/priorauth/security/model_view.py`), wrapped around the API's production system: only the pages
  that minimum necessary selects, renumbered, with identifiers redacted; the model's citations are mapped back to the
  original pages and restored before the verifier checks them against the original text.
  - De-identification (`security/redaction.py`): name and each part of it, MRN, member id, date of birth in four
    spellings, phone numbers → `[PATIENT]`, `[MRN]`, `[DOB_2]`... one placeholder per spelling, exactly reversible.
  - Minimum necessary (`security/minimum_necessary.py`): a deny-list; a page is withheld only if its heading is an
    administrative type (fax cover, scheduling note, eligibility, intake questionnaire, refill request), it has no
    term of the procedure's vocabulary and no instruction-like text. Unknown types are sent. Written from dev only.
- RBAC (`security/auth.py`): users with a UUID, a unique username, one role (`system`, `reviewer`, `admin`) and a
  scrypt hash; HS256 JWT of 15 minutes (`sub`, `role`, `iat`, `exp`, `jti`, `iss`), user re-read on every request;
  401 + `WWW-Authenticate` / 403; a `system` user only sees its own cases. `POST /auth/token`, `GET /auth/me`; demo
  users seeded from `PRIORAUTH_DEMO_PASSWORD`.
- Field encryption (`security/crypto.py`): Fernet/MultiFernet on the record, conditions, member id and result;
  key from `PRIORAUTH_FIELD_KEY` (`make field-key`); the app refuses to start without it.
- Audit (`security/audit.py`): HMAC-SHA256 chain (`PRIORAUTH_AUDIT_KEY`) over logins, failed logins, refusals,
  submissions, reads, decisions and fail-closed failures; `GET /audit`, `GET /audit/verify` (admin).
- `make minimum-necessary` → `results/minimum_necessary.json`; `make hipaa-tests` (also a CI step) runs exactly the
  tests named by the covered rows.

**Measured** (no API call in P6; `results/spend.json` unchanged)
- Pages sent to the model (`results/minimum_necessary.json`): dev 62.1 %, test 61.0 %, robust 100 % (the rule does
  not know robust's document headings, so it withholds nothing there). Every gold evidence page and every injection
  page is sent on all three splits (dev evidence pages 275/275, Wilson 95 % [0.986, 1]).
- No known identifier of the patient in any request sent to the model, for the 100 dev cases through the production
  wiring with the scripted oracle; every decision still matches gold and every restored quote verifies on the
  original page; the same scan without the model view finds the identifiers
  (`tests/test_redaction.py::test_no_identifier_and_no_withheld_page_reaches_the_model` and its control).
- Route × caller matrix, 8 routes × (no token, system, reviewer, admin); forged, re-signed, `alg=none`, foreign and
  expired tokens refused (`tests/test_auth.py`).
- A scan of every cell of the raw SQLite file after a full run finds no identifier, no page line and no quote; the
  same scan over the decrypted cells finds them (`tests/test_encryption.py`).
- Six kinds of tampering detected (modified entry, entry re-hashed without the key, deleted middle or last entry,
  relinked entry, rewound head) and 60 concurrent appends from two writers stay one chain, on SQLite and Postgres 16
  (`tests/test_audit.py`).
- `make load-test` re-run through the API's production code path (token, model view, encryption, audit) on SQLite,
  in-process, with the simulated model: 200/200 done and matching gold; timings in `results/load_test.json`. (This
  is not a production configuration: no Postgres, no network, no real model; see P7bis for the Postgres run.)
- CI run #15 (`ac68995`) green: lint, the full suite with the Postgres and Jaeger services, `make hipaa-tests`, the
  gate and the secret scan.

**Left**
- The effect of redaction and page selection on a real model's accuracy is **not measured**: no API call was made,
  and the P3 results (`results/comparison.md`) were recorded on whole, unredacted records.
- TLS, MFA, login lockout, token revocation, key management (KMS), audit export and retention: not covered (see the
  matrix and `docs/LIMITS.md` §7).

## P7 — Reviewer UI

**Done**
- Review API (`src/priorauth/api/review.py`, roles reviewer and admin): `GET /review/queue` (pended cases not yet
  approved by a reviewer: reasons, age, procedure filter), `GET /cases/{id}/review` (original pages, every criterion
  with its status, the agent's claim, verification and evidence with `[start, end)` offsets), `POST
  /cases/{id}/reviews` (approve; request_info with a reason; flag_error with criterion, corrected status and comment).
  Comments are encrypted; every action is audited. Offsets come from `agent/verifier.locate()`, i.e. the verifier's
  own normalization mapped back to the original characters.
- Feedback loop: each review appends one line (case, action, criterion, corrected status; ids and labels only, never
  the comment) to `eval/cases_from_review.jsonl`; `eval/review_to_cases.py` turns the reviewed cases into dev
  evaluation cases under `data/dev_review/` (trap `from_review`; data/dev, test and robust untouched).
- `ui/`: Next.js 16 (App Router), TypeScript strict, Tailwind 4; types generated from `docs/openapi.json` by
  openapi-typescript, client openapi-fetch, `/api/*` forwarded to FastAPI by a route handler. Pages `/login`
  (demo reviewer account), `/queue`, `/cases/[id]` (pages with highlights on the left, criteria and actions on the
  right).
- `eval/demo_api.py` (`make demo`): the real API with a **simulated model** (the scripted oracle), seeded with
  synthetic dev cases; the UI shows `scripted-oracle-not-a-model+protected` as the extractor.
- `make ui-setup`, `make ui-test` (API types up to date, `tsc --noEmit`, Vitest, `next build`), `make ui-e2e`
  (Playwright); a `ui` job in CI runs all of them.

**Measured** (no API call in P7; `results/spend.json` unchanged)
- Highlighting: every verified gold quote of the three splits is located and its span normalizes to the quote
  (`tests/test_review.py::test_every_verified_gold_quote_is_located_exactly`); on a real API response
  (`ui/tests/fixtures/case_review.json`, whose offsets a pytest checks against `locate()`), each highlight is exactly
  `page[start:end]` and every page renders its text character for character (`ui/tests/unit/HighlightedPage.test.tsx`);
  segmentation gives back the text for 500 random span sets, overlaps and astral characters included
  (`ui/tests/unit/highlight.test.ts`).
- Review rules, queue updates, encrypted comments, feedback lines and their conversion to dev cases:
  `tests/test_review.py`; review routes in the role matrix (`tests/test_auth.py`).
- Two Playwright journeys pass locally on Chromium 1194 (`@playwright/test` 1.56.1): approve a pended case, which
  leaves the queue; follow a citation to its highlight, flag an agent error, reload, see it stored
  (`ui/tests/e2e/review.spec.ts`).
- Production build without type errors (`next build` runs the TypeScript check).
- CI run #17 (`f0be52c`) green: the `ui` job ran `make ui-test` (API types up to date, `tsc`, 20 Vitest tests,
  `next build`) and `make ui-e2e` (2 Playwright journeys passed on the runner's Chromium), next to the Python job and
  the secret scan.

**Left**
- A reviewer's approval is recorded beside the engine's decision; it does not change the ClaimResponse or send a
  webhook. No assignment of cases to reviewers, no second-reviewer rule.
- The fixture and the E2E journeys use cases decided by the scripted oracle, not by a model.
- The UI lockfile was generated with npm 11: npm 10.9 crashes while resolving the peer dependencies
  (`Cannot read properties of null (reading 'edgesOut')`); `npm ci` from the lockfile works with npm 10.

## P7bis — Real-model measurement of the pipeline, demo on real responses, reviewer decisions, Postgres load test

**Done**
- `eval/pipeline.py` (`make pipeline`): agent v2 on Haiku through the API's whole path, in-process: FHIR Bundle →
  `POST /cases` → queue → worker → model view (minimum necessary + redaction) → agent → verifier on the original pages
  → engine → encrypted result. The model now sees a case id derived from the redacted content (not the API's random
  id), so the recorded responses replay. Recorded once per split with the real API, in this order: dev, test, robust;
  no rule or prompt was changed at any point. `results/pipeline_<split>.json`, `results/pipeline_<split>_errors.jsonl`
  and `results/pipeline_comparison.md` (`eval/pipeline_report.py`) are generated and replay byte for byte in tests.
- Demo (`eval/demo_api.py`, `make demo`) and the UI's E2E journeys and fixture use those recorded real responses
  (replay-only client; a case without a recording stops the demo). The scripted oracle is used by tests only.
- A reviewer's approval is the case's final decision: `POST /cases/{id}/reviews` with `approve` stores the review,
  sets the job's reviewer decision and queues a signed `case.reviewed` webhook in one transaction
  (`JobQueue.record_review`); `GET /cases/{id}` returns the final outcome with the engine's and the reviewer's; the
  ClaimResponse is certified (A1) by the reviewer and keeps the engine's PEND and reasons in its process notes; the
  stored CaseRun is never rewritten; the audit entry `review.approve` carries the new outcome. The UI shows both.
- Load test: `--database-url` (with `--reset-database`), `make load-test-postgres`; CI runs it on SQLite and on its
  Postgres 16 service, checks the deterministic part against the committed files and uploads the results.

**Measured**
- Real model through the pipeline (`results/pipeline_comparison.md`), against the same agent on whole records:
  dangerous approvals 0/65, 0/39, 0/28 on dev, test, robust (unchanged); criterion accuracy dev 0.9815 (whole record
  0.9908; 2 unjustified PENDs appear), test 0.9884 (0.9806), robust 0.9831 (0.9831); pages sent 62.1 %, 61.0 %,
  100 %; cost 0.157, 0.092, 0.068 USD (whole record 0.165, 0.100, 0.069). The dev drop is in `docs/LIMITS.md` §9.
- No patient identifier in the recorded responses (scan of the three cassette files).
- Reviewer approval: ClaimResponse, signed webhook, both decisions, audit entry, closed case, two simultaneous
  approvals giving one 201 and one 409 (`tests/test_review.py`, `tests/test_fhir.py`).
- Load test, simulated model, 200 cases, 32 workers, same GitHub runner (2 CPUs), CI run #19 (`6d1e9fe`): SQLite
  26.0 cases/s, PostgreSQL 16.15 27.41 cases/s, 200/200 done and matching gold on both. `results/load_test.json` and
  `results/load_test_postgres.json` are that run's artifact (`load-test-results`, zip sha256 `098a8e25…`), copied
  unchanged. The earlier SQLite figures of P5 and P6 came from this container, not from a runner.
- API spend of P7bis: 0.31719566 USD (ledger scopes `agent_v2_haiku_pipeline/<split>`), under the 1 USD cap; ledger
  total 6.53820259 USD.

**Left**
- One recorded run per arm: the dev, test and robust differences are within run-to-run variation (§9 of LIMITS).
- The pipeline and the load test run in-process; neither is a deployment (no network, no TLS, one process).
- A reviewer can approve a pended case but cannot pend an approved one; "request more information" does not change
  the ClaimResponse.

## Step A (P8) — Online demo

**Done**
- `priorauth.demo` (`python -m priorauth.demo`, replaces `eval/demo_api.py`): the API with its workers in-process on a
  SQLite file in a temporary directory, seeded at start with ten labelled synthetic dev examples (`demo/examples.py`:
  clean records to approve and to pend, dose x4, left/right inverted, inconsistent dates, hidden instruction,
  contradiction, missing evidence, paraphrases), each chosen among the cases agent v2 got entirely right through the
  pipeline. Decisions come from replayed real responses; a missing recording stops the server. State resets on every
  restart (said by `/demo/info`, the UI banner, the README and `docs/DEPLOY.md`).
- Routes: `GET /demo/info`, `GET /demo/examples` (public), `POST /demo/examples/{id}/run` (reviewer: a new
  submission through the queue), `POST /demo/live` (reviewer; **off** unless `DEMO_LIVE_ENABLED=true`; daily spend
  cap, per-IP rate limit, size limit, fail-closed, nothing stored); `/healthz`; `/docs`. Only the reviewer account is
  public (`demo-reviewer` / `synthetic-demo-only`); the submitting account has a random password; no admin account.
- UI: permanent banner "Demonstration on synthetic data…", demo account shown on the login page (one click), `/try`
  page with "Run the agent": decision, PEND reasons, cited text highlighted, link to the full review page; live-mode
  form with its "synthetic data only" warning when enabled.
- `docker/api.Dockerfile`, `docker/ui.Dockerfile` (base images from the public ECR mirror, pinned by digest),
  `docker-compose.demo.yml`, `make demo-up`, `make smoke-demo URL=… [UI_URL=…]` (`priorauth.demo.smoke`).
- CI job `demo-images`: builds both images, starts them, runs the smoke test through the UI proxy.
- `.github/workflows/deploy.yml` (API → Azure Container Apps with OIDC, UI → Vercel CLI, then the smoke test; nightly
  smoke test at 04:17 UTC; skipped with a notice while unconfigured), checked with actionlint 1.7.7.
  `docs/DEPLOY.md`: every manual step.

**Measured**
- `make smoke-demo URL=http://127.0.0.1:8000 UI_URL=http://127.0.0.1:3000` passed here against the two images run
  by `docker compose` (built with this environment's proxy CA passed as an optional build secret; CI builds them
  without it): healthz, docs, demo routes, `/login` and `/try`, sign-in through the UI proxy, the clean example
  APPROVED with a located citation, the hidden-instruction example PENDED with `INJECTION_DETECTED`.
- Tests: `tests/test_demo.py` (every example decided as its gold label says, through the queue; public vs reviewer
  routes; only the reviewer account usable; live mode off by default, 413, 429, 502, 503 and no row stored; the demo
  refuses to start without recordings; the smoke test passes and catches an approved hidden instruction), Vitest
  `DecisionView`, Playwright journey on the Try page (3 journeys in total).
- Live mode run once with the real model on a short synthetic MRI record: 2 calls, 0.00168177 USD (printed by the
  run; the live mode's ledger is in memory and not part of `results/spend.json`).

**Left**
- Not deployed: no access to Azure or Vercel from here. `deploy.yml` has never run; the commands of `docs/DEPLOY.md`
  have not been executed.
- One replica only (state per container); the live mode's counters reset on restart; the live mode cannot redact
  names from free text (no structured header), hence the "synthetic data only" warning.
