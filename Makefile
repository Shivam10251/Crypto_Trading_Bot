# Thin wrappers around the underlying tools. Every target is a command you can
# also run by hand - nothing here hides behaviour.
.DEFAULT_GOAL := help
.PHONY: help install up down logs api web test test-backend test-frontend lint typecheck check migrate revision clean

BACKEND  := backend
FRONTEND := frontend

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install backend and frontend dependencies
	cd $(BACKEND) && uv venv --python 3.12 && uv pip install -e '.[dev]'
	cd $(FRONTEND) && pnpm install

up: ## Start PostgreSQL
	docker compose up -d

down: ## Stop PostgreSQL (keeps data)
	docker compose down

logs: ## Tail PostgreSQL logs
	docker compose logs -f postgres

api: ## Run the backend API
	cd $(BACKEND) && uv run trading-bot-api

web: ## Run the frontend dev server
	cd $(FRONTEND) && pnpm dev

test: test-backend test-frontend ## Run all tests

test-backend: ## Run backend tests
	cd $(BACKEND) && uv run pytest

test-frontend: ## Run frontend tests
	cd $(FRONTEND) && pnpm test

lint: ## Lint and format-check
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .

typecheck: ## Type-check backend and frontend
	cd $(BACKEND) && uv run mypy
	cd $(FRONTEND) && pnpm typecheck

check: lint typecheck test ## Everything CI would run

migrate: ## Apply database migrations
	cd $(BACKEND) && uv run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add markets"
	cd $(BACKEND) && uv run alembic revision --autogenerate -m "$(m)"

clean: ## Remove build and cache artifacts
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache
	rm -rf $(FRONTEND)/dist $(FRONTEND)/node_modules/.vite
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
