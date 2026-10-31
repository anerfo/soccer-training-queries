# Overview

`soccer-training-queries` is a small Python daemon, CLI and web UI for running
a WhatsApp group for a soccer team. It uses
[neonize](https://github.com/krypton-byte/neonize) (whatsmeow) with your own
WhatsApp account.

> The official WhatsApp Cloud API cannot send to groups, so this project uses
> the unofficial multi-device protocol. Automation can get a number banned.
> Prefer a secondary number for the bot.

## Features

- Send polls and text messages to a group.
- Schedule messages from public iCalendar feeds (Google Calendar), including
  recurring events, cancellations and a monthly calendar view.
- Collect poll votes, map phone numbers to names and export results to Excel.
- Optional OpenID Connect login (Authelia) and a bearer-protected JSON API.
- Daemon with a Unix socket, a web UI and CLI commands for cron.

## How it works

- `wa.py serve` keeps the WhatsApp session connected, prints a QR code for
  pairing, listens on `data/wa.sock` and serves the web UI on
  `http://127.0.0.1:8080`.
- The calendar scheduler sends configured polls or texts relative to events
  from public ICS feeds.
- `wa.py send` and `wa.py poll-next` can be used from the CLI or cron.
- The session is stored in `data/wa_session.db`, so you only scan the QR once.

## Documentation

- [Setup & first run](setup.md)
- [Web UI](web-ui.md)
- [Calendar-driven messages](calendar.md)
- [Polls, results & contacts](polls.md)
- [Authentication (OIDC / Authelia)](authentication.md)
- [Security](security.md)
- [CLI & cron](cli.md)
- [Development](development.md)
- [Troubleshooting](troubleshooting.md)

## Project layout

- `wa.py` — CLI entrypoint (also `python -m soccer` / `soccer-bot`)
- `soccer/daemon.py` — daemon, Unix socket server and CLI commands
- `soccer/web.py` — Flask app and pages
- `soccer/auth.py` — OIDC authentication
- `soccer/calendar/` — iCalendar data (`data.py`) and scheduler
- `templates/`, `static/`, `tests/` — UI assets and tests

Set `APP_HOME` if the package is installed outside the source tree so it can
find `polls.txt`, `templates/` and `static/`.
