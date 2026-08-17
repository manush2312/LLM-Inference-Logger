# Developer entrypoints. Everything a contributor needs is one `make` away,
# and CI runs the same targets -- so "works on my machine" and "passes CI" are
# the same statement.

SHELL := /bin/bash
# --env-file is not optional here. Compose looks for `.env` in the *compose
# file's* directory (infra/), not the repo root, so without this every
# ${VAR:-default} silently falls back to its default -- including the API keys
# the README tells you to put in .env. Surgical on purpose: --project-directory
# would also move relative build-context resolution.
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
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
check: lint types test web-test ## Run every gate

.PHONY: web-test
web-test: ## Frontend tests (rendering-over-time behaviour)
	cd frontend && npm run test

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

# ---------------------------------------------------------------------------
# Full stack
# ---------------------------------------------------------------------------
.PHONY: up
up: .env ## Build and run all five services
	$(COMPOSE) up --build -d
	@echo "frontend  http://localhost:5173"
	@echo "api docs  http://localhost:8000/docs"

.PHONY: down
down: ## Stop all services (keeps volumes)
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail logs from every service
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

# ---------------------------------------------------------------------------
# Kubernetes (local, via kind)
#
# KCTX is passed to EVERY kubectl call below. A developer's active context is
# often a real cluster; pinning it here means an unqualified apply can never
# reach one by accident.
# ---------------------------------------------------------------------------
KIND_CLUSTER := llm-logger
# The ingress controller is pinned AND vendored, for two separate reasons.
#
# Pinned, not `main`: fetching an unpinned branch means the controller can change
# under you between two runs of the same command, which is the opposite of what a
# reproducible deploy is for.
#
# Vendored: `make k8s-deploy` used to fetch this at deploy time, which makes a
# GitHub outage or rate limit a failure of *this* project. That is not
# hypothetical -- raw.githubusercontent.com returned 429 for this IP mid-way
# through a verification run and blocked the deploy entirely. Anyone evaluating
# this repo should not need GitHub to be reachable and unthrottled. The file is
# fetched once into vendor/ and committed; the fetch below is only the bootstrap
# path for refreshing it, and it caches into the same place.
INGRESS_VERSION := controller-v1.11.3
INGRESS_URL := https://raw.githubusercontent.com/kubernetes/ingress-nginx/$(INGRESS_VERSION)/deploy/static/provider/kind/deploy.yaml
KCTX := --context kind-$(KIND_CLUSTER)
K8S := infra/k8s
# Declared after K8S, not before: `:=` expands immediately, so referencing
# $(K8S) above its definition silently yields a path with an empty prefix.
INGRESS_VENDORED := $(K8S)/vendor/ingress-nginx-$(INGRESS_VERSION).yaml

.PHONY: lint-actions
lint-actions: ## Lint the GitHub Actions workflows (needs Docker)
# Not part of `check`, which must run without Docker. Worth having as its own
# target because a workflow file is only validated when GitHub parses it, so an
# invalid one sits undetected until a push -- which is exactly what happened: an
# `if: hashFiles(...)` guard was invalid from M1 and went unnoticed for the whole
# build because there was no remote to reject it.
	@docker run --rm -v "$$PWD:/repo" --workdir /repo rhysd/actionlint:latest -color \
		&& echo "actionlint: clean"

.PHONY: kind-up
kind-up: ## Create the local cluster and install an ingress controller
	kind create cluster --config $(K8S)/kind-cluster.yaml
	$(MAKE) $(INGRESS_VENDORED)
	kubectl $(KCTX) apply -f $(INGRESS_VENDORED)
	kubectl $(KCTX) -n ingress-nginx wait --for=condition=available deploy/ingress-nginx-controller --timeout=180s
# `deployment available` is not the same as `admission webhook serving`:
# applying an Ingress in that gap fails with a connection-refused webhook error.
# Poll the admission endpoint until it actually has an address -- `kubectl wait
# --selector` is no good here, it errors out immediately when nothing matches yet.
	@for i in $$(seq 1 60); do \
		addrs=$$(kubectl $(KCTX) -n ingress-nginx get endpointslices \
			-l kubernetes.io/service-name=ingress-nginx-controller-admission \
			-o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null); \
		if [ -n "$$addrs" ]; then echo "admission webhook ready at $$addrs"; break; fi; \
		sleep 2; \
	done

# A file target, not .PHONY: once vendor/ is committed this never runs again.
$(INGRESS_VENDORED):
	@echo "vendoring ingress-nginx $(INGRESS_VERSION) (one time)..."
	@mkdir -p $(dir $@)
	@for i in 1 2 3; do \
		curl -fsSL -o $@.tmp $(INGRESS_URL) && mv $@.tmp $@ && break; \
		rm -f $@.tmp; \
		echo "  fetch failed (attempt $$i/3), retrying in 15s..."; sleep 15; \
	done
	@test -s $@ || { echo "ERROR: could not fetch $(INGRESS_URL)"; \
		echo "  raw.githubusercontent.com may be rate-limiting this IP (429)."; \
		echo "  Retry later, or drop the file at $@ by hand."; exit 1; }

.PHONY: kind-load
kind-load: ## Build both images and load them into the cluster
	docker build -t llm-inference-logger-backend:dev ./backend
	docker build -t llm-inference-logger-frontend:dev ./frontend
	kind load docker-image llm-inference-logger-backend:dev --name $(KIND_CLUSTER)
	kind load docker-image llm-inference-logger-frontend:dev --name $(KIND_CLUSTER)

.PHONY: k8s-apply
k8s-apply: ## Apply the manifests to the local cluster
# Retried once: even after the ingress controller pod reports ready, kube-proxy
# may not have programmed the route to its admission webhook yet, and the
# Ingress apply fails with connection-refused. Observed, not hypothetical.
	kubectl $(KCTX) apply -k $(K8S) || (sleep 20 && kubectl $(KCTX) apply -k $(K8S))
	kubectl $(KCTX) -n llm-logger rollout status deploy/backend --timeout=180s
	kubectl $(KCTX) -n llm-logger rollout status deploy/worker --timeout=180s
	kubectl $(KCTX) -n llm-logger rollout status deploy/frontend --timeout=180s
	@echo "app: http://localhost:8080"

.PHONY: k8s-status
k8s-status: ## Show what is running in the cluster
	kubectl $(KCTX) -n llm-logger get pods,svc,ingress,hpa

.PHONY: k8s-logs
k8s-logs: ## Tail worker logs
	kubectl $(KCTX) -n llm-logger logs -l app=worker -f --tail=50

.PHONY: k8s-deploy
k8s-deploy: kind-up kind-load k8s-apply ## Full local Kubernetes run, from nothing

.PHONY: kind-down
kind-down: ## Delete the local cluster
	kind delete cluster --name $(KIND_CLUSTER)
