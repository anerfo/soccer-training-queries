PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff
MYPY ?= .venv/bin/mypy
COVERAGE ?= .venv/bin/coverage
RADON ?= .venv/bin/radon
PYLINT ?= .venv/bin/pylint
BANDIT ?= .venv/bin/bandit
PIP_AUDIT ?= .venv/bin/pip-audit

.PHONY: install-dev test lint format format-check typecheck coverage complexity maintainability duplication metrics security quality

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

complexity:
	$(RADON) cc -s -a wa.py web.py auth.py calendar_sync.py calendar_data.py calendar_scheduler.py

maintainability:
	$(RADON) mi -s wa.py web.py auth.py calendar_sync.py calendar_data.py calendar_scheduler.py

duplication:
	$(PYLINT) --disable=all --enable=duplicate-code --min-similarity-lines=8 wa.py web.py auth.py calendar_sync.py calendar_data.py calendar_scheduler.py

metrics: complexity maintainability duplication

security:
	$(BANDIT) -r wa.py web.py auth.py calendar_sync.py calendar_data.py calendar_scheduler.py -q
	$(PIP_AUDIT) -r requirements.txt

quality: lint format-check typecheck test coverage metrics security
