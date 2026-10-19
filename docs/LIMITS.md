# Limits

What this evaluation does not show, what still fails, and what is not built. Every number points to a committed
file; the per-system numbers are in [`results/comparison.md`](../results/comparison.md), generated from
`results/<system>_<split>.json`, and the failures are in `results/<system>_<split>_errors.jsonl`.

## 1. Small samples

- dev has 100 cases, test 60, robust 40. Each rate is reported with its 95 % Wilson interval: "0 dangerous
  approvals" means 0/39 on test (interval up to 9.0 %) and 0/28 on robust (up to 12.1 %), not "never".
- Prompt injection: 8 cases in dev, 3 in test, 4 in robust. Every LLM system flags all of them, but 3/3 on test
  only bounds the detection rate below at 43.9 % (field `injection_detection` of each results file).
- Each system was recorded once per split. Run-to-run variance is not measured: the models reject any
  non-default sampling value (`temperature=0` is a 400 on claude-haiku-5-5 and claude-sonnet-5-5), so a
  re-recording can give different numbers. Results are reproducible only through the cassettes.

## 2. Same author, synthetic data

- One person wrote the data templates (dev, test and robust), the gold labels, the rules of baseline 1 and the
  prompts. Baseline 1 is therefore optimistic on dev and test (its rules expect exactly the template wording)
  and collapses on robust: it pends all 12 approvable cases and flags none of the 4 injections
  (`results/rules_baseline_robust.json`); its 0/28 dangerous approvals there only mean it approves nothing.
- data/robust was written after agent@v2 and baseline 1 were frozen, with new templates (no gold quote of robust
  occurs in dev or test, `tests/test_robust.py`), but by the same author, who had read the dev errors. It tests
  new wording, not an independent view of what real records look like.
- The records are clean synthetic text. Real faxes have handwriting, scan artefacts, tables, many more pages and
  ambiguous clinical language; the OCR noise in robust is a simple character-substitution model.
- Some labels are judgment calls. Example: in `paraphrase` cases, "latent TB blood test and hepatitis B panel ...
  came back clear" is labelled MET for the screening criterion (an interferon-gamma release assay is the blood
  test for latent TB). agent@v1 and baseline 2 read the policy literally and pend these cases
  (`results/agent_v1_haiku_dev_errors.jsonl`, `results/llm_single_haiku_test_errors.jsonl`).

## 3. What still fails

From the `*_errors.jsonl` files of the systems on test and robust (never used to change a prompt):

- **Side mismatch recorded as a conflict.** Apart from one citation error (next point), every remaining criterion
  error of agent v2 (Haiku and Sonnet) on test and robust is a `laterality_swap` case where the model records
  NOT_MET *and* sets `conflict`, which the engine turns into INSUFFICIENT. The decision is still the expected PEND.
  The same pattern exists on dev (2 cases for Haiku, 4 for Sonnet, `results/agent_v2_*_dev_errors.jsonl`), but it
  was analysed only after test and robust had been run; a v3 prompt written now would be tuned with knowledge of
  test and robust, so it was not written.
- **A wrong citation costs a reviewer's time.** agent v2 (Haiku) cited a page header on the wrong page in one
  approvable test case (`test-038`); the verifier rejected the quote, the criterion became INSUFFICIENT and the
  case was pended. Fail-closed by design, but it is an unjustified PEND.
- **Literal reading of the policy** (baseline 2 and agent v1): equivalent wording not accepted, an omission on one
  page treated as a contradiction, the side difference also counted against the diagnosis criterion. agent@v2
  addresses these on dev; baseline 2 kept its first prompt (see 4).

## 4. What the comparison does and does not isolate

- agent@v2 benefited from one round of error analysis on dev; baseline 2 (`single@v1`) did not. The like-for-like
  comparison of architecture is baseline 2 against agent v1, both with first prompts written before any run.
- The Sonnet agent uses agent@v2, written from the errors of the Haiku agent; Sonnet was not tuned separately and
  its effort level was not swept (fixed at `medium`, like Haiku).
- **Citation verification is not semantic verification.** The verifier checks that a quote exists on the cited
  page, not that it supports the status. The gate fixture shows the consequence: a prompt with a stale, looser
  policy summary produces 10/65 dangerous approvals on dev while 721/722 of its citations are valid
  (`results/agent_degraded_haiku_dev.json`). The deterministic engine guarantees that nothing unverified is
  approved; it cannot catch a verified quote judged against the wrong threshold.
- A deliberately lenient prompt ("lean toward MET, do not report notes addressed to reviewers") did not degrade
  Haiku 5.5 on dev: 0/65 dangerous approvals, 8/8 injections still flagged
  (`results/agent_lenient_haiku_dev.json`). Part of the observed safety therefore comes from the model's own
  behaviour, which this project does not control and which can change with a model version.

## 5. Cost, latency and cache measurements

- Prices were read on 2026-10-19 from the published price list (`src/priorauth/llm/pricing.py`); costs are
  computed from the usage returned by the API and summed in `results/spend.json`. Every Haiku prompt stayed below
  the 100,000-token threshold of its higher rate card.
- The very first key-check call (16 input and 4 output tokens, 0.0000036 USD) was made before the ledger existed
  and is not in `results/spend.json`. Debugging runs on small subsets are in the ledger (scopes `_scratch/...`),
  but their cassettes are not committed. The ledger scope `agent_degraded_haiku/dev` also holds the recording of
  the lenient prompt, made under that name before it was renamed.
- Latency is the sum of the model calls of a case as recorded, from one machine through a proxy, 8 cases in
  parallel; tool execution is excluded. It is not a production latency.
- The first wave of parallel cases of a run cannot read the shared system-and-tools cache entry (a cache entry is
  readable only once the first response starts); the cost per case includes that.

## 6. Not done in P3

- The evaluated systems read whole synthetic records, names included: page selection and de-identification came in
  P6 and only wrap the API's production system (§7); every number in `results/comparison.md` is without them.
- No OpenTelemetry traces, audit log, API or job queue in P3 (added in P4 to P6).
- `eval-live.yml` has **never run**, on GitHub or anywhere else: its secret check, cost-cap check and artifact
  upload are untested and must not be read as validated. It needs the repository secret `ANTHROPIC_API_KEY`. All
  recorded results came from local `LLM_MODE=record` runs.
- The gate runs on dev only; test and robust are reported, never gated.
- No agreement study between the gold labels and a clinician; no shoulder cases in the robust PT templates.

## 7. Security controls (P6)

Aligned with the principles of the HIPAA Security Rule, synthetic data, no claim of compliance or certification, no
BAA with anyone. What each control does not do:

- **Model view, measured once with a real model (P7bis), see §9.** In P6 it was checked with the scripted oracle only.
- **The page-selection rule was written from dev and sends 100 % of the pages on robust.** It is a keyword deny-list
  built from the dev document headings; on robust, whose headings it does not know, it withholds nothing: 224 of 224
  pages sent, against 62.1 % on dev and 61.0 % on test (`results/minimum_necessary.json`). Test shares dev's
  generator, so its 100 % evidence recall says little about real records. It cannot read scanned images, only text.
- **De-identification** removes only the identifiers the structured header announces (and phone numbers): a
  misspelled or OCR-damaged name, an address, a relative's name, provider names, NPIs and service dates stay. The
  residual check in `results/minimum_necessary.json` looks for the same identifiers, so its zero is by construction;
  a fuzzy search for near-misses of patient names in the text sent found none on the three splits (two false
  positives, a clinician's name and an ordinary word). This is not Safe Harbor.
- **Authentication**: no MFA, no lockout or rate limit on failed logins, no revocation list (15-minute tokens, user
  re-read on each request), no user-management API (users are created in code; demo users from an env password).
  Without `PRIORAUTH_JWT_SECRET` the secret is random per process.
- **Encryption at rest**: key in an environment variable, no KMS or HSM, no automatic re-encryption after a key
  rotation, no disk, database or backup encryption. The Bundle hash used for idempotency is stored in clear.
- **Audit log**: written after the action commits (a crash in between loses that entry); append-only by code only (no
  WORM storage, no external anchor); anyone holding both the HMAC key and write access to the database can rewrite
  it; without `PRIORAUTH_AUDIT_KEY` it is unkeyed (reported as `keyed: false`). `verify` reads the whole chain.
- **Transmission**: no TLS in the application; webhook URLs are not forced to HTTPS.

## 8. Reviewer UI (P7)

- The demo (`eval/demo_api.py`) shows real model findings, but replayed: claude-haiku-5-5 responses recorded once on
  dev by `eval/pipeline.py` (P7bis). Only the 100 dev cases have recordings; a new record cannot be decided without
  `LLM_MODE=record` or `live` and a key. The UI says which system extracted the evidence.
- A reviewer's approval is final for the ClaimResponse and the webhook (P7bis) but only approvals are: a reviewer
  cannot turn an engine APPROVE into PEND, and "request more information" is recorded without changing the
  ClaimResponse (it stays pended). There is no second-reviewer rule.
- Reviewers see whole records with identifiers (no field masking for people); the review comment is free text,
  encrypted at rest but shown to every reviewer and admin.
- The queue is computed in memory from every pended job (no pagination in the database).
- A highlight marks the first place where the quote occurs on its page; a quote repeated on the same page is marked
  once. A quote that the verifier accepted but whose normalization differs character by character (combining
  accents) is shown as "not located" rather than highlighted wrongly.
- Feedback cases from reviews keep the agent's evidence only where the reviewer did not change the status; a
  corrected criterion has no evidence in its gold label.
- No accessibility audit, no internationalisation, desktop layout first.

## 9. The API pipeline measured with a real model (P7bis)

`results/pipeline_comparison.md` compares agent v2 on Haiku reading whole, unredacted records (P3 harness) with the
same agent through the API pipeline (minimum-necessary pages, redacted identifiers), one recorded run each.

- **On dev, accuracy went down with the pipeline**: criterion accuracy 425/433 = 0.9815 against 429/433 = 0.9908,
  decision accuracy 98/100 against 100/100, two unjustified PENDs (dev-059, dev-093) where there were none. In both,
  the model quoted the page summary that `list_pages` returns ("Group | Rheumatology Follow-up", two heading lines
  joined) as if it were page text; the verifier rejected the quote and the criterion fell to INSUFFICIENT. Five
  laterality criteria expected NOT_MET were reported NOT_MET with `conflict: true` and kept as INSUFFICIENT (still
  pended). On test the pipeline did better (0.9884 against 0.9806), on robust the same (0.9831).
- Dangerous approvals stayed at 0 on all three splits (0/65, 0/39, 0/28).
- One run per arm, no repetition: the model cannot be run at temperature 0, so the differences above are within what
  run-to-run variation alone could produce (the Wilson intervals overlap on every split). They are reported, not
  claimed as an effect of the pipeline.
- Nothing was changed after looking at test or robust; nothing was changed after dev either (the dev errors above
  were analysed, not fixed).
- The pipeline ran in-process on SQLite (the same code as the API, not a deployment).
