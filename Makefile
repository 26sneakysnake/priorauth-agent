# Targets are added phase by phase (see PLAN.md); a target only exists once it works.
.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help setup lint fmt test data data-check data-pdf

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

data: ## Regenerate data/dev and data/test (refuses to change the frozen test split)
	$(UV) run python -m priorauth.data.generator --out data

data-check: ## Fail if data/ differs byte for byte from a fresh generation
	$(UV) run python -m priorauth.data.generator --out data --check

data-pdf: ## Optional: export dev cases to build/pdf/dev/*.pdf (not committed)
	$(UV) run python -m priorauth.data.pdf --split dev --out build/pdf
