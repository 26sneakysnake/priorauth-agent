# Targets are added phase by phase (see PLAN.md); a target only exists once it works.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test data data-check data-pdf eval-offline gate readme report eval-live secrets run field-key openapi load-test load-test-postgres minimum-necessary pipeline hipaa-tests audit ui-setup ui-test ui-e2e demo demo-up demo-down smoke-demo up down

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Install the locked environment and the git pre-commit hook
	$(UV) sync --locked
	@if [ -d .git ]; then $(UV) run pre-commit install; fi

lint: ## ruff (lint + format check) and mypy --strict
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fmt: ## Auto-fix lint issues and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test: ## Run the test suite
	$(UV) run pytest

data: ## Regenerate data/dev, data/test and data/robust (refuses to change the frozen test and robust splits)
	$(UV) run python -m priorauth.data.generator --out data
	$(UV) run python -m priorauth.data.robust --out data

data-check: ## Fail if data/ differs byte for byte from a fresh generation
	$(UV) run python -m priorauth.data.generator --out data --check
	$(UV) run python -m priorauth.data.robust --out data --check

data-pdf: ## Optional: export dev cases to build/pdf/dev/*.pdf (not committed)
	$(UV) run python -m priorauth.data.pdf --split dev --out build/pdf

eval-offline: ## Replay every committed result (rules + recorded LLM cassettes, no API key); writes results/
	$(UV) run python -m eval.run --offline-all
	$(UV) run python -m eval.report

readme: ## Regenerate README.md from results/ (never edit README.md by hand)
	$(UV) run python -m eval.readme

report: ## Regenerate results/comparison.md from results/*.json
	$(UV) run python -m eval.report

SYSTEM ?= agent_v2_haiku
SPLIT ?= dev
MAX_COST_USD ?= 0.50
eval-live: ## Paid live run (needs PRIORAUTH_ANTHROPIC_API_KEY): make eval-live SYSTEM=... SPLIT=... MAX_COST_USD=...
	LLM_MODE=live $(UV) run python -m eval.run --system $(SYSTEM) --split $(SPLIT) --max-cost-usd $(MAX_COST_USD) --out live-results

gate: ## Regression gate: replay the production agent on dev, compare with results/baseline_gate.json
	$(UV) run python -m eval.gate

secrets: ## Secret scan of every commit and of the working tree (needs gitleaks >= 8.28 on PATH, as in CI)
	gitleaks git --redact --no-banner --exit-code 1 .
	gitleaks dir --redact --no-banner --exit-code 1 .

AUDIT_REPO ?= $(shell git remote get-url origin)
AUDIT_REF ?= main
audit: ## Fresh clone (AUDIT_REPO, AUDIT_REF=main): setup lint test eval-offline gate, clean tree, data checks, gitleaks on every commit
	scripts/audit.sh $(AUDIT_REPO) $(AUDIT_REF)

run: ## API + workers on SQLite; needs PRIORAUTH_FIELD_KEY (make field-key), see priorauth.api.app for the rest
	@test -n "$$PRIORAUTH_FIELD_KEY" || { echo "PRIORAUTH_FIELD_KEY is not set: export PRIORAUTH_FIELD_KEY=\$$(make -s field-key)"; exit 1; }
	$(UV) run uvicorn priorauth.api.app:app --host 127.0.0.1 --port 8000

field-key: ## Print a new Fernet key for PRIORAUTH_FIELD_KEY (never commit it)
	@$(UV) run python -m priorauth.security.crypto

openapi: ## Export the API schema to docs/openapi.json
	$(UV) run python -m priorauth.api.openapi

load-test: ## 200 cases at once against the API with a SIMULATED model, on SQLite; writes results/load_test.json
	$(UV) run python -m eval.load_test

load-test-postgres: ## Same on Postgres (LOAD_TEST_DATABASE_URL, its tables are dropped); results/load_test_postgres.json
	@test -n "$$LOAD_TEST_DATABASE_URL" || { echo "set LOAD_TEST_DATABASE_URL (a Postgres URL whose tables may be dropped)"; exit 1; }
	$(UV) run python -m eval.load_test --database-url "$$LOAD_TEST_DATABASE_URL" --reset-database --out results/load_test_postgres.json

pipeline: ## Replay agent v2 through the API pipeline on every split (real-model cassettes) + results/pipeline_comparison.md
	for s in dev test robust; do $(UV) run python -m eval.pipeline --split $$s || exit 1; done
	$(UV) run python -m eval.pipeline_report

hipaa-tests: ## Run exactly the tests named by the "covered" rows of docs/HIPAA_ALIGNMENT.md
	$(UV) run pytest -q $$($(UV) run python -c "from tests.test_hipaa_alignment import covered_test_ids; print(' '.join(covered_test_ids()))")

minimum-necessary: ## Pages sent to the model and identifiers redacted, per split; writes results/minimum_necessary.json
	$(UV) run python -m eval.minimum_necessary

ui-setup: ## Install the UI dependencies from ui/package-lock.json
	cd ui && npm ci --no-audit --no-fund

ui-test: ## UI: generated API types up to date, TypeScript strict, Vitest, production build
	cd ui && npm run check:api && npm run typecheck && npm test && npm run build

ui-e2e: ## UI: two Playwright journeys against the demo API (SIMULATED model) and the build of ui-test
	cd ui && npx playwright test

demo-up: ## Build and start the demo images (API :8000, UI :3000); state resets on every restart
	docker compose -f docker-compose.demo.yml up -d --build --wait

demo-down: ## Stop the demo containers
	docker compose -f docker-compose.demo.yml down

smoke-demo: ## Smoke test of a running demo: make smoke-demo URL=<api url> [UI_URL=<ui url>]
	@test -n "$(URL)" || { echo "usage: make smoke-demo URL=<api base url> [UI_URL=<ui base url>]"; exit 1; }
	$(UV) run python -m priorauth.demo.smoke --api "$(URL)" $(if $(UI_URL),--ui "$(UI_URL)")

demo: ## Demo API on :8000 (examples decided by replayed real responses); then `cd ui && npm run dev`
	$(UV) run python -m priorauth.demo --port 8000

up: ## Postgres + Jaeger (docker compose); then e.g. OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 make run
	docker compose up -d

down: ## Stop the compose services
	docker compose down
