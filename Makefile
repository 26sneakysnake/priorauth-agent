# Targets are added phase by phase (see PLAN.md); a target only exists once it works.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test data data-check data-pdf eval-offline gate report eval-live secrets

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
