# Developer entrypoints. Everything a contributor needs is one `make` away,
# and CI runs the same targets -- so "works on my machine" and "passes CI" are
# the same statement.

SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker-compose.yml
BACKEND := backend
# --directory makes uv both resolve the project and run the command from
# backend/, so tools that take relative paths (mypy, pytest, alembic) behave
# the same from the repo root as they do from inside backend/.
UV := uv run --directory $(BACKEND)

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
.PHONY: setup
setup: .env ## Install backend + frontend dependencies
	uv sync --directory $(BACKEND) --extra dev
	@if [ -d frontend/package.json ] || [ -f frontend/package.json ]; then \
		cd frontend && npm install; \
	fi

.env: ## Create .env from the template on first run
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------
.PHONY: infra-up
infra-up: .env ## Start Postgres + Redis and wait until healthy
	$(COMPOSE) up -d
	@echo "waiting for services to report healthy..."
	@$(COMPOSE) ps

.PHONY: infra-down
infra-down: ## Stop backing services (keeps volumes)
	$(COMPOSE) down

.PHONY: infra-nuke
infra-nuke: ## Stop backing services AND delete their data
	$(COMPOSE) down -v

.PHONY: psql
psql: ## Open a psql shell against the dev database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-llm} -d $${POSTGRES_DB:-llm_logger}

.PHONY: redis-cli
redis-cli: ## Open a redis-cli shell against the dev instance
	$(COMPOSE) exec redis redis-cli

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
.PHONY: api
api: ## Run the API with hot reload
	$(UV) llm-api

.PHONY: worker
worker: ## Run the ingestion worker
	$(UV) llm-worker

.PHONY: web
web: ## Run the frontend dev server
	cd frontend && npm run dev

# ---------------------------------------------------------------------------
# Quality gates -- `make check` is what CI runs
# ---------------------------------------------------------------------------
.PHONY: check
check: lint types test ## Run every gate

.PHONY: lint
lint: ## Lint and format-check
	$(UV) ruff check .
	$(UV) ruff format --check .

.PHONY: fmt
fmt: ## Auto-format and auto-fix
	$(UV) ruff format .
	$(UV) ruff check --fix .

.PHONY: types
types: ## Static type check
	$(UV) mypy app

.PHONY: test
test: ## Unit tests (no external services required)
	$(UV) pytest -m "not integration"

.PHONY: test-all
test-all: ## Full suite including integration tests (needs `make infra-up`)
	$(UV) pytest

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply all migrations
	$(UV) alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add foo"
	$(UV) alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	$(UV) alembic downgrade -1

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
