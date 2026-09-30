API := apps/api

.PHONY: install lint typecheck test test-pg up down migrate run

install:        ## install backend with dev deps
	cd $(API) && pip install -e ".[dev]"
lint:
	cd $(API) && ruff check . && ruff format --check .
typecheck:
	cd $(API) && mypy
test:           ## fast suite on SQLite
	cd $(API) && pytest --cov
test-pg:        ## suite on the compose Postgres
	cd $(API) && SF_TEST_DATABASE_URL=postgresql+asyncpg://solutionforge:solutionforge@localhost:5432/solutionforge_test pytest --cov
up:
	docker compose up --build -d
down:
	docker compose down
migrate:
	cd $(API) && alembic upgrade head
run:
	cd $(API) && uvicorn solutionforge.main:app_factory --factory --reload
