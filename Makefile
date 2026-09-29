# Developer shortcuts. Every target runs inside Docker, so only Docker is needed.
.PHONY: up down test lint

# Build images and start Postgres, Redis, both mock partners, the API and the worker.
up:
	docker compose up --build -d

# Stop the stack and delete its data volumes.
down:
	docker compose down -v

# Run unit + integration tests in the api image against the compose Postgres and Redis.
# The suite drops and recreates its own database (loyalty_test) on every run.
test:
	docker compose run --rm --build api python -m pytest

# Lint (ruff), formatting check (ruff format) and strict type checking (mypy --strict).
lint:
	docker compose run --rm --build --no-deps api sh -c "ruff check . && ruff format --check . && mypy"
