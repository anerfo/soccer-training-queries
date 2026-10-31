PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
COVERAGE ?= .venv/bin/coverage
RADON ?= .venv/bin/radon
PYLINT ?= .venv/bin/pylint
BANDIT ?= .venv/bin/bandit
PIP_AUDIT ?= .venv/bin/pip-audit
MKDOCS ?= .venv/bin/mkdocs
SOURCES ?= wa.py soccer

.PHONY: install-dev test lint format format-check typecheck coverage complexity maintainability duplication metrics security docs-serve docs-build quality

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

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

complexity:
	$(RADON) cc -s -a $(SOURCES)

maintainability:
	$(RADON) mi -s $(SOURCES)

duplication:
	$(PYLINT) --disable=all --enable=duplicate-code --min-similarity-lines=8 $(SOURCES)

metrics: complexity maintainability duplication

security:
	$(BANDIT) -r $(SOURCES) -q
	$(PIP_AUDIT) -r requirements.txt

docs-serve:
	$(MKDOCS) serve

docs-build:
	$(MKDOCS) build --strict

quality: lint format-check typecheck test coverage metrics security
