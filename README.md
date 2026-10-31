# soccer-training-queries

WhatsApp bot for a soccer group: send polls, collect yes/no votes, and schedule
messages from public calendar feeds. Uses the unofficial WhatsApp multi-device
protocol (neonize/whatsmeow) — prefer a secondary number for the bot.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python wa.py serve   # scan data/qr.png, then open http://127.0.0.1:8080
```

Docker:

```bash
WA_UID=$(id -u) WA_GID=$(id -g) docker compose up -d --build
```

## Documentation

The full documentation lives in [`docs/`](docs/index.md):

- [Setup & first run](docs/setup.md)
- [Web UI](docs/web-ui.md)
- [Calendar-driven messages](docs/calendar.md)
- [Polls, results & contacts](docs/polls.md)
- [Authentication (OIDC / Authelia)](docs/authentication.md)
- [Security](docs/security.md)
- [CLI & cron](docs/cli.md)
- [Development](docs/development.md)
- [Troubleshooting](docs/troubleshooting.md)
