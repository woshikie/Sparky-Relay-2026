# Relay — day to day.
#
# The shell scripts this replaced were build.sh, run.sh, bot.sh and a bit of
# README prose. Three files that each did one thing, each needing a chmod, each
# a separate thing to remember.

SHELL := /bin/sh
PY := .venv/bin/python
COMPOSE := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
IMAGE := sparky-relay-2026:latest
CONTAINER := relay

.DEFAULT_GOAL := help
.PHONY: help setup check check-verbose build up down restart logs status \
        verify shell clean distclean

help: ## show this
	@echo "Relay - make targets"
	@echo
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sed 's/:.*## /\t/' \
	  | awk -F'\t' '{printf "  %-14s %s\n", $$1, $$2}'
	@echo
	@echo "  first run:  make setup  &&  vi secrets.env  &&  make check  &&  make up"

setup: ## venv, deps, geckodriver (bare metal only)
	./setup.sh

$(PY): ## create the venv and install deps
	@echo "==> venv"
	python3 -m venv .venv
	@echo "==> deps"
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements.txt -r requirements-dev.txt
	@echo "==> geckodriver"
	./setup.sh --driver-only
	@touch $@

check: $(PY) ## run the test suite with coverage
	$(PY) -m pytest tests/ --cov=. --cov-report=term-missing:skip-covered

check-verbose: $(PY) ## same, but per-test
	$(PY) -m pytest tests/ -v --cov=. --cov-report=term-missing:skip-covered

build: ## build the container image
	@test -n "$(COMPOSE)" || { echo "no podman or docker found" >&2; exit 1; }
	$(COMPOSE) build --format docker -t $(IMAGE) .
	$(COMPOSE) images $(IMAGE) --format '{{.Repository}}:{{.Tag}}  {{.Size}}'

up: build ## build and start detached
	./run.sh up -d relay
	@echo
	@echo "  logs:   make logs"
	@echo "  status: make status"

down: ## stop and remove the container
	./run.sh down

restart: ## restart
	./run.sh restart relay

logs: ## follow the bot's output
	./run.sh logs -f relay

status: ## container and memory state
	./run.sh ps
	-$(COMPOSE) exec $(CONTAINER) $(PY) -c "import memory; print(memory.describe())" 2>/dev/null \
	  || echo "(container not running)"

verify: ## prove the image can read the site's OCR, without submitting
	$(COMPOSE) exec $(CONTAINER) $(PY) -u check_container.py

shell: ## a shell inside the running container
	./run.sh exec relay /bin/sh

clean: ## remove caches, coverage output and the local ledger
	rm -rf .pytest_cache __pycache__ tests/__pycache__
	rm -f .coverage .coverage.*
	rm -f ledger.sqlite3 ledger.sqlite3-wal ledger.sqlite3-shm

distclean: clean ## also remove the venv and downloaded binaries
	rm -rf .venv bin