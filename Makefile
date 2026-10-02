.PHONY: up down logs test test-local lint format typecheck migrate check

COMPOSE_TEST = docker compose -p jokr-test -f docker-compose.yml -f docker-compose.test.yml

up:
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f

# Full suite inside compose against a throwaway database.
test:
	$(COMPOSE_TEST) build test
	$(COMPOSE_TEST) run --rm test; status=$$?; $(COMPOSE_TEST) down -v; exit $$status

# Suite on the host against the dev db (make up first). Uses POSTGRES_* from .env.
test-local:
	set -a; . ./.env; set +a; \
	TEST_DATABASE_URL=postgresql+psycopg://$$POSTGRES_USER:$$POSTGRES_PASSWORD@127.0.0.1:5432/postgres \
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

migrate:
	docker compose exec api alembic upgrade head

check: lint typecheck test
