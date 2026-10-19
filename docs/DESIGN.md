# Design

Synthetic data only. This page explains the choices that shape the code, what each one costs, and what would
change at a larger scale. Measurements are in `results/` (summarised in the generated `README.md`); failures and
what is not measured are in `docs/LIMITS.md`.

## 1. The model reads; code decides

The model never decides. It reads the record through tools and records findings: one status per policy criterion
(MET, NOT_MET, INSUFFICIENT), each with page-and-quote evidence and an optional conflict flag
(`src/priorauth/agent/tools.py`). Two deterministic steps follow:

- **The verifier** (`src/priorauth/agent/verifier.py`) checks every quote against the page it cites, after a
  normalisation of case, Unicode forms, typographic quotes and whitespace. A MET or NOT_MET claim whose evidence
  does not check out becomes INSUFFICIENT. The verifier is cheap, explainable and cannot be talked out of its rule.
  It proves that the words are on the page, not that they support the claim.
- **The policy engine** (`src/priorauth/policy/engine.py`) decides APPROVE only when every criterion of the policy
  is MET, verified, unique and conflict-free, and no injection was flagged. Anything else is PEND with
  machine-readable reasons.

**Why there is no DENY.** A denial of care is the decision with the highest cost of error, and in the US it usually carries
procedural requirements (a clinician's review, a written rationale, appeal rights) that software alone should not
meet. A system that can only approve or ask a human cannot deny by mistake. Its failure modes are a wrong approval
(measured as the dangerous-approval rate) or a wasted review (the unjustified-PEND rate). That asymmetry is
deliberate: every uncertain path (missing finding, unverified quote, conflict, model error, budget cap, timeout,
crash) ends in PEND.

## 2. A manual tool loop rather than a framework

`src/priorauth/agent/loop.py` is under 200 lines: one Messages API call per step, every tool call of a step
executed, results sent back together, at most 12 steps, stop after two steps without progress. Writing it by hand
rather than using an agent framework buys four things this project needs:

- **Replayable requests.** Each request is a pure function of the case and the prompt, so its hash keys a cassette.
  Every result in `results/` replays byte for byte in CI without a key (`src/priorauth/llm/cassette.py`).
- **Prompt caching.** The conversation is append-only and the tool list and system prompt never change, so later
  steps read the earlier ones from the cache.
- **Exact cost control.** Each call is checked against a per-case, a per-run and a phase cap before it is sent
  (`src/priorauth/llm/client.py`).
- **No hidden behaviour.** No retries the code cannot see, no summarisation of earlier steps, no tool chosen by a
  library.

The cost: features such as parallel sub-agents or memory are absent; none was needed.

## 3. A job queue in the database rather than Celery

Jobs live in a `jobs` table (`src/priorauth/jobs/queue.py`). A worker claims the oldest claimable job with
`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres, completed by an update guarded by a version number, which also
makes SQLite safe. A claimed job holds a lease renewed by a heartbeat. A crashed worker's job is reclaimed when
its lease expires. A result is written only by the lease holder. After `max_attempts` claims the job fails closed
(PEND).

Why not Celery and a broker:

- **One fewer system.** The API already needs the database; a broker adds a service to run, secure and monitor.
- **Transactional outbox.** The result, the webhook delivery and, for reviews, the reviewer's decision are written
  in one transaction, so a webhook is never sent for a result that was not stored.
- **Idempotency in the same place.** The idempotency key, the case and the job are one insert.
- **Inspectable state.** "What is running, what failed and why" is a query (`GET /cases/{id}`).

The cost: polling (a claim query every 0.2 s per idle worker) and a database that serialises claims. At the load
measured here (200 cases at once, simulated model, `results/load_test_postgres.json`) it does not matter.

## 4. Privacy is in the pipeline, not in the prompt

Before any model call, the model view (`src/priorauth/security/model_view.py`) drops administrative pages that
carry nothing clinical and replaces the patient's identifiers by placeholders. After the call it maps the
citations back to the original pages, so the verifier still checks the original text. Field encryption, role
checks and the hash-chained audit log are ordinary application code with tests
(`docs/HIPAA_ALIGNMENT.md`). None of this depends on the model behaving well.

## 5. What would change at scale

- **Throughput.** Workers become separate processes (the queue already supports it on Postgres); claims move from
  polling to `LISTEN/NOTIFY`, or to a broker once the number of workers or tenants makes the database the
  bottleneck. Model calls go through a shared rate limiter and a gateway that spreads load across keys and regions;
  the Batch API for non-urgent cases halves their cost.
- **Inputs.** Real faxes need OCR with layout (tables, handwriting, checkboxes) and page images kept for the
  reviewer; citations then carry coordinates, not only text offsets. Page selection becomes a classifier
  evaluated on real records, not a keyword list.
- **Policies.** The criteria are already data: a YAML file validated when it is loaded
  (`src/priorauth/policy/policies.yaml`). At scale they become versioned policy documents with an editor and tests
  per policy, and decisions record the policy version.
- **Evaluation.** De-identified real records labelled by clinicians, with inter-rater agreement, a frozen test set
  per policy version, repeated runs to separate model variance from real differences, and drift monitoring on
  production traffic (agreement with reviewers, PEND rate, citation failures).
- **Security and operations.** TLS everywhere, a key management service for field keys and audit keys, WORM
  storage for the audit log, SSO with MFA for reviewers, per-tenant isolation, a BAA with every subprocessor, and
  the administrative safeguards of the Security Rule. None of these is in this repository
  (`docs/HIPAA_ALIGNMENT.md`).
- **Review workflow.** Assignment, service levels, a second reviewer for overrides, and denials kept for licensed
  clinicians outside this system.
