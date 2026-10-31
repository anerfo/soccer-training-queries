PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
COVERAGE ?= .venv/bin/coverage

.PHONY: install-dev test lint format format-check typecheck coverage quality

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m unittest discover -s tests

lint:
	$(RUFF) check . --exclude .venv --exclude data

format-check:
	$(RUFF) format --check . --exclude .venv --exclude data

format:
	$(RUFF) format . --exclude .venv --exclude data

typecheck:
	$(MYPY)

coverage:
	$(COVERAGE) run -m unittest discover -s tests
	$(COVERAGE) report -m

quality: lint format-check typecheck test coverage
