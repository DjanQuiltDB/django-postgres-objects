# Development entry points. Everything real lives in pyproject.toml; these are shortcuts.

PYTHON ?= python3.14
VENV   ?= .venv
BIN    := $(VENV)/bin

# The compose file exposes Postgres on the host, so a local run can reach it without entering a container.
PG17 ?= postgresql://postgres:postgres@localhost:5443/test_db
PG18 ?= postgresql://postgres:postgres@localhost:5445/test_db

# The Python and Django pairs tested on each PostgreSQL. Each Django minor has its own pinned tox factor. Python 3.15
# runs on Django 6.1 only, because Django 5.2 and 6.0 do not support Python 3.15. PostgreSQL 17 is tested on Django 5.2
# with Python 3.14 and on Django 6.1 with Python 3.15, the newest Python each supports. The pgtrigger checks follow the
# same split, so every pair in PG18_LEGS that is not also in PG17_LEGS runs them against PostgreSQL 18, and the pairs in
# PG17_LEGS run them against PostgreSQL 17.
PG18_LEGS ?= py312-dj52 py312-dj60 py312-dj61 py313-dj52 py313-dj60 py313-dj61 py314-dj52 py314-dj60 py314-dj61 py315-dj61
PG17_LEGS ?= py314-dj52 py315-dj61

.PHONY: help venv test test-pg17 test-pg18 test-pgtrigger lint format docs build clean databases docker-test

help:
	@echo 'venv         Create $(VENV) and install the package with its dev extras'
	@echo 'databases    Start the Postgres containers the tests need'
	@echo 'test         Run the suite against every tested Python, Django and Postgres version, plus the pgtrigger checks'
	@echo 'test-pg17    Run the suite against PostgreSQL 17 only, on the newest Python of Django 5.2 and of 6.1'
	@echo 'test-pg18    Run the suite against PostgreSQL 18 only, on every Python and Django'
	@echo 'test-pgtrigger  Run the django-pgtrigger compatibility checks only, on every Python and Django'
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
	DATABASE_URL=$(PG17) $(BIN)/tox -e $(shell echo $(PG17_LEGS) | tr ' ' '\n' | sed 's/$$/-pg17/' | paste -sd,)

test-pg18: venv
	DATABASE_URL=$(PG18) $(BIN)/tox -e $(shell echo $(PG18_LEGS) | tr ' ' '\n' | sed 's/$$/-pg18/' | paste -sd,)

test-pgtrigger: venv
	DATABASE_URL=$(PG18) $(BIN)/tox -e $(shell echo $(filter-out $(PG17_LEGS),$(PG18_LEGS)) | tr ' ' '\n' | sed 's/$$/-pgtrigger/' | paste -sd,)
	DATABASE_URL=$(PG17) $(BIN)/tox -e $(shell echo $(PG17_LEGS) | tr ' ' '\n' | sed 's/$$/-pgtrigger/' | paste -sd,)

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
	HOST_UID=$(shell id -u) HOST_GID=$(shell id -g) docker compose run --rm test

clean:
	rm -rf build dist docs/_build .tox .ruff_cache htmlcov .coverage
	find src tests -name '__pycache__' -type d -prune -exec rm -rf {} +
