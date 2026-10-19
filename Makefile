# Targets are added phase by phase (see PLAN.md); a target only exists once it works.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test data data-check data-pdf eval-offline gate report eval-live secrets run field-key openapi load-test minimum-necessary hipaa-tests ui-setup ui-test ui-e2e demo up down

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

run: ## API + workers on SQLite; needs PRIORAUTH_FIELD_KEY (make field-key), see priorauth.api.app for the rest
	@test -n "$$PRIORAUTH_FIELD_KEY" || { echo "PRIORAUTH_FIELD_KEY is not set: export PRIORAUTH_FIELD_KEY=\$$(make -s field-key)"; exit 1; }
	$(UV) run uvicorn priorauth.api.app:app --host 127.0.0.1 --port 8000

field-key: ## Print a new Fernet key for PRIORAUTH_FIELD_KEY (never commit it)
	@$(UV) run python -m priorauth.security.crypto

openapi: ## Export the API schema to docs/openapi.json
	$(UV) run python -m priorauth.api.openapi

load-test: ## 200 cases at once against the API with a SIMULATED model; writes results/load_test.json
	$(UV) run python -m eval.load_test

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

demo: ## Demo API on :8000 (SIMULATED model, synthetic cases); then `cd ui && npm run dev`
	$(UV) run python -m eval.demo_api --port 8000

up: ## Postgres + Jaeger (docker compose); then e.g. OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 make run
	docker compose up -d

down: ## Stop the compose services
	docker compose down
