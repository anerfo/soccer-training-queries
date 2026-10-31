# Setup & first run

## Requirements

- Python 3.11+
- FFmpeg is optional and only needed for media messages.

## Local installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For development, install the editable package with the dev extra:

```bash
.venv/bin/pip install -e ".[dev]"
```

## Docker

Build and run the daemon:

```bash
mkdir -p data
WA_UID=$(id -u) WA_GID=$(id -g) docker compose up -d --build
docker compose logs -f wa
```

The QR code is written to `data/qr.png` on the host. The web UI is available
at <http://127.0.0.1:8080>. You can use the same image for CLI commands:

```bash
docker compose exec wa python wa.py groups
docker compose exec wa python wa.py polls
docker compose exec wa python wa.py poll-next --group "Soccer Mates"
```

`data/` on the host stores the session, socket, QR code, rotation state,
calendar configuration and calendar send state. `polls.txt` is bind-mounted
read-write so the web editor can save it. `WA_UID`/`WA_GID` make the container
user match your host user so it can write to `./data`; they default to `1000`.

The image includes `libmagic` (required by neonize) but not FFmpeg. Text
messages and polls work without FFmpeg; media messages would need it.

Stop the daemon with `docker compose down`.

## First run (pair the account)

```bash
.venv/bin/python wa.py serve
```

A QR code is printed in the terminal and saved to `data/qr.png`. On your phone
open **WhatsApp > Linked devices > Link a device** and scan it. The image file
is overwritten as the QR rotates.

Once you see `Connected to WhatsApp`, open `http://127.0.0.1:8080` to manage
polls, results, names and calendars, and leave the daemon running.

To run it in the background:

```bash
nohup .venv/bin/python wa.py serve --log-file data/daemon.log >/dev/null 2>&1 &
```

## Find the group

```bash
.venv/bin/python wa.py groups
```

Example output:

```text
Soccer Mates	120363012345678901@g.us
Family	120363098765432109@g.us
```

Group names are matched case-insensitively. If the name is ambiguous, pass the
JID instead. `wa.py groups --json` is available for scripts.

## Data files

- `data/wa_session.db` — WhatsApp session (scan the QR once)
- `data/wa.sock` — daemon Unix socket
- `data/polls_state.json` — poll rotation
- `data/poll_history.json` — sent polls and votes (last 50)
- `data/contacts.json` — number → name mapping
- `data/calendars.json` — calendars and rules
- `data/calendar_state.json` — sent occurrences and rotation
- `data/qr.png` — pairing QR code
