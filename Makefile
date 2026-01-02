SHELL := /bin/bash

.DEFAULT_GOAL := help

VENV ?= .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

COMPOSE ?= docker compose
COMPOSE_FILE ?= docker-compose.yaml
SERVICE ?=

IMAGE ?= external-dns:local

.PHONY: help \
	venv install lint format test test-integration build py-build pre-commit setup \
	run docker stack stack-dev dev docker-down docker-build start stop restart logs ps \
	template templates clean

## Show this help
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Development:"
	@echo "  venv         Create virtualenv in $(VENV)"
	@echo "  install      Install dev dependencies (editable)"
	@echo "  lint         Run ruff checks"
	@echo "  format       Run ruff formatter"
	@echo "  test         Run pytest unit tests"
	@echo "  build        Build Python sdist/wheel"
	@echo "  pre-commit   Install git hooks"
	@echo ""
	@echo "Run external-dns:"
	@echo "  run          Run external-dns locally (Python, requires .env)"
	@echo ""
	@echo "Local test stack (AdGuard + Traefik + whoami + external-dns):"
	@echo "  stack        Build image + start full local test stack (detached)"
	@echo "  stack-dev    Build + run full stack in foreground (Ctrl+C to stop)"
	@echo "  dev          Build + run external-dns only in foreground"
	@echo "  docker       Alias for stack"
	@echo "  docker-down  Stop and remove local test stack"
	@echo "  test-integration  Run integration tests against stack"
	@echo ""
	@echo "Docker compose (SERVICE optional):"
	@echo "  start        Start containers (up -d)"
	@echo "  stop         Stop containers"
	@echo "  restart      Restart containers"
	@echo "  logs         Follow logs"
	@echo "  ps           Show container status"
	@echo ""
	@echo "Config templates:"
	@echo "  template     Render *.template files (uses .env if present)"
	@echo "  clean        Remove rendered (untracked) template outputs"
	@echo ""
	@echo "Examples:"
	@echo "  make install         # Setup dev environment"
	@echo "  make run             # Run external-dns locally"
	@echo "  make stack           # Start full local test stack"
	@echo "  make logs SERVICE=external-dns"


# ----------------------
# Python development
# ----------------------

venv:
	@test -x "$(PYTHON)" || python3 -m venv $(VENV)

install: venv pre-commit
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e ".[dev]"

lint: venv
	$(VENV)/bin/ruff check .

format: venv
	$(VENV)/bin/ruff format .

test: venv
	$(VENV)/bin/pytest

test-integration: venv
	EXTERNAL_DNS_RUN_DOCKER_TESTS=1 $(VENV)/bin/pytest -o addopts= -vv -s -rA --maxfail=1 tests/integration

py-build: venv lint format
	$(PYTHON) -m build

build: py-build

pre-commit: venv
	$(VENV)/bin/pre-commit install --install-hooks
	$(VENV)/bin/pre-commit install --hook-type commit-msg

setup: pre-commit


# ----------------------
# Run locally
# ----------------------

run: venv
	@set -a; [ -f .env ] && . ./.env; set +a; \
	$(PYTHON) -m external_dns


# ----------------------
# Docker (local test stack)
# ----------------------

docker-build:
	docker build -q -t $(IMAGE) .

stack: docker-build
	@mkdir -p docker/local/adguard/conf
	@test -f docker/local/adguard/conf/AdGuardHome.yaml || cp docker/local/adguard/conf.example/AdGuardHome.yaml docker/local/adguard/conf/AdGuardHome.yaml
	@IMAGE=$(IMAGE) $(COMPOSE) -f $(COMPOSE_FILE) up -d --no-build

docker: stack

stack-dev: docker-build
	@mkdir -p docker/local/adguard/conf
	@test -f docker/local/adguard/conf/AdGuardHome.yaml || cp docker/local/adguard/conf.example/AdGuardHome.yaml docker/local/adguard/conf/AdGuardHome.yaml
	IMAGE=$(IMAGE) $(COMPOSE) -f $(COMPOSE_FILE) up --no-build

dev: docker-build
	@mkdir -p docker/local/adguard/conf
	@test -f docker/local/adguard/conf/AdGuardHome.yaml || cp docker/local/adguard/conf.example/AdGuardHome.yaml docker/local/adguard/conf/AdGuardHome.yaml
	IMAGE=$(IMAGE) $(COMPOSE) -f $(COMPOSE_FILE) up --no-build external-dns

docker-down:
	$(COMPOSE) -f $(COMPOSE_FILE) down -v

start:
	$(COMPOSE) -f $(COMPOSE_FILE) up -d $(SERVICE)

stop:
	$(COMPOSE) -f $(COMPOSE_FILE) stop $(SERVICE)

restart:
	$(COMPOSE) -f $(COMPOSE_FILE) restart $(SERVICE)

logs:
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f $(SERVICE)

ps:
	$(COMPOSE) -f $(COMPOSE_FILE) ps


# ----------------------
# Config templates
# ----------------------

templates: template

template:
	@command -v envsubst >/dev/null 2>&1 || { echo "envsubst is required (macOS: brew install gettext && brew link --force gettext)"; exit 1; }
	@echo "Rendering templates..."
	@set -a; [ -f .env ] && . ./.env; set +a; \
	rendered=0; \
	for tpl in $$(find config docker -type f -name "*.template" 2>/dev/null); do \
		out=$$(echo "$$tpl" | sed 's/\.template$$//'); \
		tmp=$$(mktemp); \
		vars_in_tpl=$$(grep -o '\\$${[A-Z_][A-Z0-9_]*}' "$$tpl" 2>/dev/null || true); \
		envsubst < "$$tpl" > "$$tmp"; \
		if [ -n "$$vars_in_tpl" ]; then \
			for var in $$vars_in_tpl; do \
				name=$$(echo "$$var" | sed 's/\\$${\\([^}]*\\)}/\\1/'); \
				eval "val=\$$$$$name"; \
				if [ -z "$$val" ]; then \
					echo "ERROR: $$var is not set (needed by $$tpl)"; \
					rm -f "$$tmp"; \
					exit 1; \
				fi; \
			done; \
		fi; \
		mv "$$tmp" "$$out"; \
		chmod 644 "$$out"; \
		rendered=$$((rendered + 1)); \
	done; \
	echo "Rendered $$rendered template(s)"

clean:
	@echo "Removing rendered (untracked) template outputs..."
	@removed=0; \
	for tpl in $$(find config docker -type f -name "*.template" 2>/dev/null); do \
		out=$$(echo "$$tpl" | sed 's/\.template$$//'); \
		[ -f "$$out" ] || continue; \
		if command -v git >/dev/null 2>&1 && git ls-files --error-unmatch "$$out" >/dev/null 2>&1; then \
			continue; \
		fi; \
		rm -f "$$out"; \
		removed=$$((removed + 1)); \
	done; \
	echo "Removed $$removed file(s)"
