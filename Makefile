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

# Backend code lives under backend/; the virtualenv stays at the project root so
# one interpreter serves scripts, tests and the API regardless of which tree they
# sit in. BE prefixes any command that must run with backend/ as its directory.
BE := cd backend &&
PYTHONPATH_BE := PYTHONPATH=backend

.DEFAULT_GOAL := help
.PHONY: help venv up down logs migrate seed test lint typecheck preflight probe \
        evidence pin-digests fps-guard audit sbom clean record-demo record-demo-gateway \
        gateway-ingest gateway-report compare-recognisers deploy-secrets

help:  ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtualenv and install pinned dependencies
	python -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r backend/requirements-dev.txt

up:  ## Start the stack and wait for health
	$(COMPOSE) up -d --wait

down:  ## Stop the stack, preserving volumes
	$(COMPOSE) down

logs:  ## Tail stack logs
	$(COMPOSE) logs -f --tail=100

migrate:  ## Apply Alembic migrations to head
	$(BE) ../$(PY) -m alembic upgrade head

seed:  ## Load departments, sites and the camera coordinate seed
	$(BE) ../$(PY) -m services.registry.seed

test:  ## Run the test suite with coverage
	$(BE) ../$(PY) -m pytest tests -q --cov=services --cov-report=term-missing

lint:  ## Static lint
	$(BE) ../$(PY) -m ruff check services scripts tests
	$(PY) -m ruff format --check services scripts tests

typecheck:  ## Strict type checking of the service code
	$(PY) -m mypy --strict services

fps-guard:  ## Assert exactly one CAP_PROP_FPS read exists in the codebase
	$(BE) ../$(PY) scripts/check_fps_guard.py

audit:  ## Check dependencies for known vulnerabilities
	$(PY) -m pip_audit -r requirements.txt --strict

sbom:  ## Generate a CycloneDX SBOM
	$(PY) -m cyclonedx_py requirements requirements.txt -o reports/sbom.json

preflight:  ## Verify the organiser's §2.4 checklist against the live gateway
	$(BE) ../$(PY) scripts/preflight_check.py --seconds 10 --emit-evidence

probe:  ## Measure real stream properties for every catalogued camera
	$(BE) ../$(PY) scripts/probe_catalogue.py --seconds 8 --sequential --emit-evidence

evidence: preflight probe  ## Regenerate every submission evidence artefact

pin-digests:  ## Resolve floating image tags in docker-compose.yml to digests
	$(BE) ../$(PY) scripts/pin_digests.py --write

clean:  ## Remove caches and ad-hoc reports (evidence records are preserved)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -f reports/*.json

# ---------------------------------------------------------------- console

frontend:  ## Install console dependencies and produce a production build
	cd frontend && npm install --no-fund --no-audit && npm run build

frontend-dev:  ## Run the console dev server (proxies /api to the API)
	cd frontend && npm run dev

gen-api:  ## Regenerate the console's TypeScript types from the live OpenAPI schema
	cd frontend && npm run gen:api

deploy-secrets:  ## Generate deployment secrets into a gitignored deploy-secrets.env
	$(BE) ../$(PY) scripts/generate_deploy_secrets.py

record-demo:  ## First-draft demo video (own-feed). Human review + narration still required.
	cd frontend && node scripts/record_demo.mjs

record-demo-gateway:  ## First-draft demo video against government-feed data.
	cd frontend && SETU_DEMO_VARIANT=gateway node scripts/record_demo.mjs

screenshots:  ## Capture every console screen against real data into docs/screenshots
	cd frontend && node scripts/capture_screenshots.mjs

# ---------------------------------------------------------------- runtime

api:  ## Run the API on 127.0.0.1:8090
	$(BE) ../$(PY) -m uvicorn services.api.main:app --host 127.0.0.1 --port 8090

watchlist:  ## Seed a representative watchlist from plates present in the footage
	$(BE) ../$(PY) scripts/seed_watchlist.py --reset

# ---------------------------------------------------------------- demo

demo: up migrate app-role seed  ## Full demo from a clean checkout. Works with the gateway down.
	@echo ""
	@echo "==> ingesting own-feed footage across replay cameras"
	$(BE) ../$(PY) scripts/seed_demo.py --reset
	@echo "==> seeding the representative watchlist"
	$(BE) ../$(PY) scripts/seed_watchlist.py --reset
	@echo "==> matching detections and raising alerts"
	$(PY) -c "import sys; sys.path.insert(0, '.'); 	  from services.analytics.matcher import scan_detections; 	  from services.api.db import get_sessionmaker; 	  s = get_sessionmaker()(); st = scan_detections(s); s.commit(); s.close(); 	  print(f'  {st.alerts_created} alert(s) raised, {st.movement} grouped as movement')"
	@echo "==> building the console"
	cd frontend && npm install --no-fund --no-audit --silent && npm run build
	@echo ""
	@echo "  Demo ready."
	@echo "    API      http://127.0.0.1:8090/docs   (make api)"
	@echo "    Console  http://localhost:5173         (make frontend-dev)"
	@echo ""
	@echo "  Credentials are in .env (SETU_ADMIN_PASSWORD / SETU_OPERATOR_PASSWORD)."
	@echo "  See docs/DEMO_RUNBOOK.md for the demonstration script."
	@echo ""

demo-reset:  ## Clear detections and alerts, then re-ingest
	$(BE) ../$(PY) scripts/seed_demo.py --reset
	$(BE) ../$(PY) scripts/seed_watchlist.py --reset

# ---------------------------------------------------------------- evidence

gateway-ingest:  ## Run ANPR across every catalogued government camera (live feed)
	$(BE) ../$(PY) scripts/ingest_gateway.py --seconds 40 --persist

gateway-report:  ## Merge gateway ingest passes into the government-feed output report
	$(BE) ../$(PY) scripts/gateway_report.py

compare-recognisers:  ## Score candidate OCR models against the annotated crops
	$(BE) ../$(PY) scripts/compare_recognisers.py

ground-truth:  ## Write the ANPR annotation sheet for a human to fill in
	$(BE) ../$(PY) scripts/ground_truth.py annotate

accuracy:  ## Score ANPR against the human annotations
	$(BE) ../$(PY) scripts/ground_truth.py score --emit-evidence

benchmark:  ## Measure journey latency, decode-to-alert and throughput
	$(BE) ../$(PY) scripts/benchmark.py --emit-evidence

detection-report:  ## Detected vehicles and plates with timestamps (CSV + PDF)
	$(BE) ../$(PY) scripts/detection_report.py --emit-evidence

app-role:  ## Create the unprivileged database role that RLS depends on
	$(BE) ../$(PY) scripts/create_app_role.py
