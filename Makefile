# Project SETU — task runner.
#
# Every target is also runnable directly (see the command it wraps), because `make` is
# not present on a stock Windows box and two of the three team machines are Windows.
# The Makefile is the CI contract; the underlying commands are the fallback.

SHELL := /bin/bash
PY    := .venv/bin/python
PIP   := .venv/bin/pip
ifeq ($(OS),Windows_NT)
	PY  := .venv/Scripts/python.exe
	PIP := .venv/Scripts/pip.exe
endif

COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help venv up down logs migrate seed test lint typecheck preflight probe \
        evidence pin-digests fps-guard audit sbom clean

help:  ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtualenv and install pinned dependencies
	python -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements-dev.txt

up:  ## Start the stack and wait for health
	$(COMPOSE) up -d --wait

down:  ## Stop the stack, preserving volumes
	$(COMPOSE) down

logs:  ## Tail stack logs
	$(COMPOSE) logs -f --tail=100

migrate:  ## Apply Alembic migrations to head
	$(PY) -m alembic upgrade head

seed:  ## Load departments, sites and the camera coordinate seed
	$(PY) -m services.registry.seed

test:  ## Run the test suite with coverage
	$(PY) -m pytest tests -q --cov=services --cov-report=term-missing

lint:  ## Static lint
	$(PY) -m ruff check services scripts tests
	$(PY) -m ruff format --check services scripts tests

typecheck:  ## Strict type checking of the service code
	$(PY) -m mypy --strict services

fps-guard:  ## Assert exactly one CAP_PROP_FPS read exists in the codebase
	$(PY) scripts/check_fps_guard.py

audit:  ## Check dependencies for known vulnerabilities
	$(PY) -m pip_audit -r requirements.txt --strict

sbom:  ## Generate a CycloneDX SBOM
	$(PY) -m cyclonedx_py requirements requirements.txt -o reports/sbom.json

preflight:  ## Verify the organiser's §2.4 checklist against the live gateway
	$(PY) scripts/preflight_check.py --seconds 10 --emit-evidence

probe:  ## Measure real stream properties for every catalogued camera
	$(PY) scripts/probe_catalogue.py --seconds 8 --sequential --emit-evidence

evidence: preflight probe  ## Regenerate every submission evidence artefact

pin-digests:  ## Resolve floating image tags in docker-compose.yml to digests
	$(PY) scripts/pin_digests.py --write

clean:  ## Remove caches and ad-hoc reports (evidence records are preserved)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -f reports/*.json
