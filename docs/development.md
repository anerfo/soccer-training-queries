# Development

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
make quality      # lint, format check, type check, tests, coverage, security
```

For the documentation site:

```bash
.venv/bin/pip install -e ".[dev,docs]"
make docs-serve   # local preview on http://127.0.0.1:8000
make docs-build   # strict build into site/
```

## Project layout

- `wa.py` — CLI entrypoint (also `python -m soccer` / `soccer-bot`)
- `soccer/daemon.py` — daemon, Unix socket server and CLI commands
- `soccer/web.py` — Flask app and pages
- `soccer/auth.py` — OIDC authentication
- `soccer/calendar/data.py` — iCalendar fetching, parsing, config and cache
- `soccer/calendar/scheduler.py` — calendar-driven scheduler
- `templates/`, `static/` — UI assets
- `tests/` — unit and integration tests
- `docs/` — this documentation

Set `APP_HOME` if the package is installed outside the source tree so it can
find `polls.txt`, `templates/` and `static/`.

## Quality targets

| Target | Purpose |
| --- | --- |
| `make test` | unit and integration tests |
| `make lint` | Ruff lint |
| `make format` / `make format-check` | Ruff formatting |
| `make typecheck` | Mypy |
| `make coverage` | coverage report (60 % gate) |
| `make complexity` | Radon cyclomatic complexity |
| `make maintainability` | Radon maintainability index |
| `make duplication` | Pylint duplicate-code |
| `make metrics` | complexity + maintainability + duplication |
| `make security` | Bandit + pip-audit |
| `make quality` | all of the above |

All tool configuration lives in `pyproject.toml` (Ruff, Mypy, Coverage,
dependencies and the `dev`/`docs` extras).

## Configuration

Runtime paths can be changed with the CLI flags (`--db`, `--qr`, `--polls`,
`--poll-history`, `--contacts`, `--calendars`, `--calendar-state`, …) or the
corresponding environment defaults. See `wa.py serve --help`.
