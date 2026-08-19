# Development entry points. Everything real lives in pyproject.toml; these are shortcuts.

PYTHON ?= python3.14
VENV   ?= .venv
BIN    := $(VENV)/bin

# The compose file exposes Postgres on the host, so a local run can reach it without entering a container.
PG17 ?= postgresql://postgres:postgres@localhost:5443/test_db
PG18 ?= postgresql://postgres:postgres@localhost:5445/test_db

.PHONY: help venv test test-pg17 test-pg18 test-pgtrigger lint format docs build clean databases docker-test

help:
	@echo 'venv         Create $(VENV) and install the package with its dev extras'
	@echo 'databases    Start the Postgres containers the tests need'
	@echo 'test         Run the suite against both Postgres versions, plus the pgtrigger compatibility checks'
	@echo 'test-pg17    Run the suite against PostgreSQL 17 only'
	@echo 'test-pg18    Run the suite against PostgreSQL 18 only'
	@echo 'test-pgtrigger  Run the django-pgtrigger compatibility checks only'
	@echo 'lint         Check linting and formatting'
	@echo 'format       Apply formatting and safe lint fixes'
	@echo 'docs         Build the documentation'
	@echo 'build        Build the sdist and wheel and check the metadata'
	@echo 'docker-test  Run everything inside the container, as CI does'
	@echo 'clean        Remove build artefacts and caches'

$(BIN)/tox:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip setuptools wheel
	$(BIN)/pip install -e '.[dev]'

venv: $(BIN)/tox

databases:
	docker compose up -d postgres17 postgres18

test: test-pg17 test-pg18 test-pgtrigger

test-pg17: venv
	DATABASE_URL=$(PG17) $(BIN)/tox -e py314-dj60-pg17

test-pg18: venv
	DATABASE_URL=$(PG18) $(BIN)/tox -e py314-dj60-pg18

test-pgtrigger: venv
	DATABASE_URL=$(PG17) $(BIN)/tox -e py314-dj60-pgtrigger

lint: venv
	$(BIN)/tox -e ruff

format: venv
	$(BIN)/ruff check --fix src tests docs
	$(BIN)/ruff format src tests docs

docs: venv
	$(BIN)/tox -e docs

build: venv
	$(BIN)/tox -e build

docker-test:
	docker compose run --rm test

clean:
	rm -rf build dist docs/_build .tox .ruff_cache htmlcov .coverage
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +
