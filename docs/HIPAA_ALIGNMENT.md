# HIPAA Security Rule: alignment matrix

**Aligned with the principles of the HIPAA Security Rule. Synthetic data only. No claim of compliance or
certification.** This project has not been assessed or audited by anyone. There is no Business Associate Agreement
(BAA) with any party, including the model provider. Every record in this repository is generated (`data/`, see the
README). The matrix maps a few technical safeguards of 45 CFR 164.312, plus 164.502(b) and 164.514, to code and to
the tests that exercise it. A row is **covered** only if it names a code file and a test that passes in CI;
`tests/test_hipaa_alignment.py` checks that every named file and test exists. Everything else is **not covered**.
Covering a row means a mechanism exists and is tested, not that the requirement is met for a real deployment.

## Technical safeguards (45 CFR 164.312)

| Reference | Control | Status | Code | Tests |
|---|---|---|---|---|
| 164.312(a)(1) | Access control: every route but `/health` and `/auth/token` needs a bearer token and a role (`system`, `reviewer`, `admin`); 401 without a valid token, 403 with the wrong role | covered | `src/priorauth/security/auth.py`, `src/priorauth/api/app.py` | `tests/test_auth.py::test_every_route_enforces_its_roles` |
| 164.312(a)(1) | Least privilege between submitters: a `system` user only sees the cases it submitted (404 otherwise) | covered | `src/priorauth/api/app.py` | `tests/test_auth.py::test_a_system_user_only_sees_the_cases_it_submitted` |
| 164.312(a)(2)(i) | Unique user identification: one UUID per user, unique usernames; the audit log records the UUID | covered | `src/priorauth/security/auth.py`, `src/priorauth/security/audit.py` | `tests/test_auth.py::test_users_have_unique_ids_and_usernames`, `tests/test_audit.py::test_the_api_records_accesses_refusals_and_decisions` |
| 164.312(a)(2)(ii) | Emergency access procedure | not covered | | |
| 164.312(a)(2)(iii) | Automatic logoff: tokens expire after 15 minutes and are not refreshed; a deactivated user is refused at once | covered | `src/priorauth/security/auth.py` | `tests/test_auth.py::test_tokens_carry_the_claims_and_expire`, `tests/test_auth.py::test_a_deactivated_user_is_refused_at_once` |
| 164.312(a)(2)(iv) | Encryption at rest: record, conditions, member id and result encrypted with Fernet before they reach the database; no key, no start; key rotation | covered | `src/priorauth/security/crypto.py`, `src/priorauth/jobs/db.py` | `tests/test_encryption.py::test_raw_database_holds_no_record_content`, `tests/test_encryption.py::test_without_a_key_nothing_is_stored_and_the_app_does_not_start`, `tests/test_encryption.py::test_key_rotation_reads_old_rows_and_wrong_keys_fail_without_echo` |
| 164.312(a)(2)(iv) | Key management (KMS or HSM, automated re-encryption), database and backup encryption | not covered | | |
| 164.312(b) | Audit controls: logins, failed logins, refused accesses, case submissions and reads, result reads, cost and audit reads, decisions and fail-closed failures | covered | `src/priorauth/security/audit.py`, `src/priorauth/api/app.py`, `src/priorauth/jobs/queue.py` | `tests/test_audit.py::test_the_api_records_accesses_refusals_and_decisions`, `tests/test_audit.py::test_raw_audit_rows_are_plain_but_hold_no_record_content` |
| 164.312(b) | Audit retention, export to a separate store, alerting | not covered | | |
| 164.312(c)(1) | Integrity of the audit log: HMAC-SHA256 hash chain with a head row; `GET /audit/verify` detects modified, deleted, relinked and truncated entries | covered | `src/priorauth/security/audit.py` | `tests/test_audit.py::test_tampering_is_detected`, `tests/test_audit.py::test_concurrent_writers_keep_one_contiguous_chain` |
| 164.312(c)(2) | Mechanism to authenticate stored ePHI: every encrypted field carries an HMAC; a modified ciphertext is an error, never a wrong value | covered | `src/priorauth/security/crypto.py` | `tests/test_encryption.py::test_a_tampered_ciphertext_is_rejected_not_misread` |
| 164.312(d) | Person or entity authentication: scrypt password hashes, HS256 JWT with sub, role, iat, exp, jti, iss; forged, re-signed, `alg=none`, foreign and expired tokens refused | covered | `src/priorauth/security/auth.py` | `tests/test_auth.py::test_password_hashes_are_salted_and_checked`, `tests/test_auth.py::test_forged_tampered_and_foreign_tokens_are_rejected`, `tests/test_auth.py::test_login_failures_are_401_whatever_the_cause` |
| 164.312(d) | MFA, lockout or rate limit on failed logins, password policy beyond 12 characters | not covered | | |
| 164.312(e)(1) | Transmission security: TLS on the API | not covered (the app serves plain HTTP; TLS would be terminated in front of it, not done yet) | | |
| 164.312(e)(2)(i) | Integrity of outbound transmissions: webhooks signed with HMAC-SHA256 over timestamp and body, with replay tolerance | covered | `src/priorauth/jobs/webhooks.py` | `tests/test_jobs.py::test_signature_round_trip_and_replay_protection`, `tests/test_api.py::test_end_to_end_bundle_to_claim_response_and_signed_webhook` |
| 164.312(e)(2)(ii) | Encryption in transit (webhook URLs are not forced to HTTPS; calls to the model provider use the SDK's HTTPS) | not covered | | |

## Uses and disclosures

| Reference | Control | Status | Code | Tests |
|---|---|---|---|---|
| 164.502(b) | Minimum necessary to the model: administrative pages without any term of the procedure's vocabulary are not sent; share of pages sent and evidence pages kept measured per split in `results/minimum_necessary.json` | covered | `src/priorauth/security/minimum_necessary.py`, `src/priorauth/security/model_view.py`, `eval/minimum_necessary.py` | `tests/test_minimum_necessary.py::test_committed_measurement_is_reproducible_and_dev_loses_no_evidence`, `tests/test_minimum_necessary.py::test_only_administrative_pages_without_clinical_content_are_withheld`, `tests/test_redaction.py::test_no_identifier_and_no_withheld_page_reaches_the_model` |
| 164.502(b) | Minimum necessary for people: role-based views, field-level masking for reviewers | not covered (a reviewer sees whole cases) | | |
| 164.514 | De-identification before any model call: name and its parts, MRN, member id, date of birth (4 spellings), phone numbers replaced by placeholders, restored only after the model answers | covered | `src/priorauth/security/redaction.py`, `src/priorauth/security/model_view.py` | `tests/test_redaction.py::test_every_known_identifier_is_replaced_and_restored_exactly`, `tests/test_redaction.py::test_model_view_of_every_case_holds_no_identifier_and_is_reversible`, `tests/test_redaction.py::test_no_identifier_and_no_withheld_page_reaches_the_model` |
| 164.514(b)(2) | Safe Harbor (all 18 identifiers): service dates, provider and facility names, NPIs, addresses and identifiers the header does not announce are kept | not covered | | |
| 164.514(b)(1) | Expert determination | not covered | | |

## Other safeguards in the code

| Reference | Control | Status | Code | Tests |
|---|---|---|---|---|
| design rule | No record content in logs or trace attributes: allowlisted span attributes and log fields, exception types only, database driver loggers pinned to WARNING, SQL parameters hidden | covered | `src/priorauth/obs/tracing.py`, `src/priorauth/obs/logs.py`, `src/priorauth/jobs/db.py` | `tests/test_observability.py::test_no_record_content_in_logs_or_spans`, `tests/test_observability.py::test_attributes_outside_the_allowlist_are_refused` |
| design rule | The model never decides alone: a deterministic engine decides APPROVE or PEND, never deny; unverified quotes are downgraded | covered | `src/priorauth/policy/engine.py`, `src/priorauth/agent/verifier.py` | `tests/test_engine.py`, `tests/test_verifier.py` |

## Not in scope

Administrative safeguards (164.308: risk analysis, workforce training, contingency plans), physical safeguards
(164.310), Business Associate Agreements (164.504(e)), breach notification (164.400-414), the Privacy Rule beyond the
two rows above. They concern an organisation and its operations, not a code repository.
