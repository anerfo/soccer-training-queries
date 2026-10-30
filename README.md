# soccer-training-queries

A small Python daemon and CLI for sending messages to a WhatsApp group (for
example, a soccer group). It uses [neonize](https://github.com/krypton-byte/neonize)
(whatsmeow) and your own WhatsApp account.

> The official WhatsApp Cloud API cannot send to groups, so this project uses
> the unofficial multi-device protocol. Automation can get a number banned.
> Prefer a secondary number for the bot.

## How it works

- `wa.py serve` keeps the WhatsApp session connected, prints a QR code for
  pairing, listens on a local Unix socket (`data/wa.sock`), and serves a web
  interface at `http://127.0.0.1:8080`.
- The calendar scheduler sends configured polls or texts relative to events
  from public ICS feeds.
- `wa.py send ...` and `wa.py poll-next ...` can still be used from the CLI or
  cron.
- The session is stored in `data/wa_session.db`, so you only scan the QR once.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

FFmpeg is optional; it is only needed for media messages.

## Docker

Build and run the daemon:

```bash
mkdir -p data
WA_UID=$(id -u) WA_GID=$(id -g) docker compose up -d --build
docker compose logs -f wa
```

The QR code is written to `data/qr.png` on the host. Scan it with WhatsApp.
The web UI is available at <http://127.0.0.1:8080>; it is mapped to localhost
only. You can also use the same image for the CLI:

```bash
docker compose exec wa python wa.py groups
docker compose exec wa python wa.py polls
docker compose exec wa python wa.py poll-next --group "Soccer Mates"
```

`data/` on the host stores the session, socket, QR code, rotation state,
calendar configuration and calendar send state. `polls.txt` is bind-mounted read-write so the web editor can save
it. `WA_UID`/`WA_GID` make the container user match your host user so it can
write to `./data`; they default to `1000`.

The image includes `libmagic` (required by neonize) but not FFmpeg. Text
messages and polls work without FFmpeg; media messages would need it.

Stop the daemon with `docker compose down`.

## Web interface

`wa.py serve` starts the web UI on `http://127.0.0.1:8080` by default. Use
`--no-web` to disable it, or `--web-host`/`--web-port` to change the bind
address. The UI has these pages:

- **Übersicht** — connection status, a preview of the next poll, calendar
  counts and a "send now" button.
- **Umfragen** — edit `polls.txt` in the browser. The list is validated before
  saving and a preview is shown below the editor.
- **Ergebnisse** — browse the most recently sent polls, see who accepted and
  declined, and export a table for Excel. See below.
- **Namen** — map phone numbers to names in an editable table. Numbers and
  names are filled automatically from the configured group and the WhatsApp
  contact store (the phone's address book); missing names can be typed in.
  The WhatsApp display name (push name) is shown under each number as a hint.
  The mapping lives in `data/contacts.json`.
- **Kalender** — attach public Google Calendar ICS feeds and send WhatsApp
  messages/polls relative to events. See below.

### Calendar-driven messages

Add a calendar with its public ICS URL (Google Calendar: *Settings → Integrate
calendar → Secret address in iCal format*, or the `…/public/basic.ics` link).
No OAuth is required; only public feeds are read.

Rules match events by title (`contains`, `exact` or `regex`) and define:

- the target group,
- the message type: a poll from `polls.txt` or a free text message,
- the offset before the event (for example `20h`, `2d`, `90m`),
- an optional **rotation list** for polls (one question per line, in order;
  empty means rotate through all questions in `polls.txt`).

Recurring events are expanded with their `RRULE`; deleting a single occurrence
in Google Calendar (or marking it cancelled) removes it from the feed, so no
message is sent for it. The feed is re-fetched right before sending to honour
last-minute deletions. The **Vorschau** table shows the upcoming sends and the
poll rotation. Calendar configuration lives in `data/calendars.json`, send
state in `data/calendar_state.json`.

### Poll results

The daemon records every poll it sends together with the votes it receives
(`data/poll_history.json`, last 50 polls). The **Ergebnisse** page lets you:

- step through the most recent polls (newer/older),
- see per-option totals and who voted for what,
- click an answer to filter the table and the Excel export,
- open **Namen** to map phone numbers to names; the list is prefilled from the
  configured group and the WhatsApp contact store, editable per row,
- copy or download just the names (one per line, no header) for the current
  filter directly into Excel.

Votes are read from WhatsApp's poll update messages, so the daemon must be
running and connected when the group votes. Votes cast on a poll that was sent
before the daemon stored it (or with a session/database that no longer holds
the poll's message secret) cannot be decrypted and are skipped.

While WhatsApp is not connected, the dashboard shows a **Simulationsdaten
hinzufügen** button below the QR code. It creates a test poll (using a question
from `polls.txt`) with random yes/no votes from made-up members, so the
**Ergebnisse** and **Namen** pages can be tried without a real account. A
**Simulationsdaten löschen** button removes those test polls again; real polls
are kept.

### Authentication (OpenID Connect / Authelia)

The app uses Authelia (or any OIDC provider) for login:

- **Browser pages** use the OIDC authorization-code flow with PKCE. Visiting a
  page while signed out redirects to `/login`, which sends you to Authelia. After
  login you return to `/auth/callback`, the ID token is validated, and the user
  is stored in a signed session cookie. The top bar shows the user and an
  **Abmelden** link (`/logout`, with RP-initiated logout when Authelia offers an
  `end_session_endpoint`).
- **JSON API** calls (`/api/...`) are bearer-only and **never** use the session.
  Send `Authorization: Bearer <jwt>`; otherwise they return `401` with
  `WWW-Authenticate: Bearer`.
- Protected pages without a session redirect to `/login`; `/health` and
  `/static/` are exempt.

Configure it with environment variables (Docker Compose forwards them):

| Variable | Meaning |
| --- | --- |
| `OIDC_ISSUER` | Provider issuer URL, e.g. `https://auth.example.com` |
| `OIDC_CLIENT_ID` | OIDC client id; used as audience if `OIDC_AUDIENCE` is unset |
| `OIDC_CLIENT_SECRET` | Client secret (omit for a public PKCE-only client) |
| `OIDC_REDIRECT_URI` | Exact redirect URI registered in Authelia; defaults to `<app>/auth/callback` |
| `OIDC_AUDIENCE` | Expected `aud` claim for API bearer tokens (optional override) |
| `OIDC_SCOPES` | Requested scopes (default `openid profile email`) |
| `OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` (default) or `client_secret_post`; must match Authelia |
| `OIDC_LOGOUT_URL` | Provider logout page when there is no `end_session_endpoint` (Authelia: `https://<authelia>/logout`) |
| `OIDC_JWKS_URI` | JWKS URL; discovered from the issuer if unset |
| `OIDC_ALGORITHMS` | Accepted signing algorithms (default `RS256,ES256,PS256`) |
| `OIDC_LEEWAY` | Allowed clock skew in seconds (default `30`) |
| `SESSION_SECRET` | Secret used to sign the session cookie (set this!) |
| `SESSION_COOKIE_SECURE` | Set to `1` when serving over HTTPS |
| `AUTH_DISABLED` | Set to `1` to turn authentication off explicitly |

Register `<app-base-url>/auth/callback` as the redirect URI for the client in
Authelia. Authentication is enforced once `OIDC_ISSUER` and `OIDC_CLIENT_ID` are
set. Without them the daemon logs a warning and serves without auth (useful for
local development); set `AUTH_DISABLED=1` to silence that on purpose.

The web UI can send messages, so keep it bound to localhost unless you protect
it. In Docker, the port is published as `127.0.0.1:8080:8080` for this reason.

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

## Send a message

```bash
.venv/bin/python wa.py send --group "Soccer Mates" --text "Match at 7pm"
```

Check the group resolution without sending:

```bash
.venv/bin/python wa.py send --group "Soccer Mates" --text "test" --dry-run
```

You can also pipe the text or use a file:

```bash
echo "Match at 7pm" | .venv/bin/python wa.py send --group "Soccer Mates"
.venv/bin/python wa.py send --group "Soccer Mates" --text-file reminder.txt
```

Set defaults with environment variables:

```bash
export WA_GROUP="Soccer Mates"
export WA_SOCKET="/opt/docker/soccer-training-queries/data/wa.sock"
```

## Recurring training questionnaire

The weekly polls live in `polls.txt`, one poll per line. Every poll is about
the Wednesday AH training and has exactly two answers:

```text
# Frage | Antwort 1 | Antwort 2
AH-Training am Mittwoch – bist du dabei? | Ich bin am Start | Ich bin raus
🏟️ „Und da ist er wieder, der AH-Kicker!“ – bist du dabei? | Da bin ich | Da bin ich nicht
```

Edit the file whenever you want; the daemon does not need a restart. The bot
walks through the list in order and wraps around, so consecutive weeks get a
different message:

```bash
# Send the next poll from polls.txt
.venv/bin/python wa.py poll-next --group "Soccer Mates"

# Preview the list and see which one is next
.venv/bin/python wa.py polls

# Check the next choice without sending or advancing the rotation
.venv/bin/python wa.py poll-next --group "Soccer Mates" --dry-run
```

The rotation is stored in `data/polls_state.json`. Delete that file to start
again from the first line. Add `--random` to `poll-next` if you prefer a random
poll that is never the same as the previous one.

You can still send a one-off poll:

```bash
.venv/bin/python wa.py poll --group "Soccer Mates" \
  --question "AH-Training am Mittwoch – bist du dabei?" \
  --option "Ich bin am Start" --option "Ich bin raus"
```

The daemon also reads the poll votes and stores them for the **Ergebnisse**
page. The session database keeps the poll message secrets, so votes can only be
decrypted while the same session stays paired and the poll is still known to it.

## Cron

The **Kalender** page is the recommended way to schedule recurring polls,
reports and reminders. Cron is still useful for one-off sends: it only needs
the `send` or `poll-next` command, and the daemon must already be running.

```cron
# Every Saturday at 10:00, remind the group about the match.
0 10 * * 6 cd /opt/docker/soccer-training-queries && .venv/bin/python wa.py send --group "Soccer Mates" --text "Match today at 7pm!" >> data/send.log 2>&1

# Every Thursday at 18:00, send the next training poll.
0 18 * * 4 cd /opt/docker/soccer-training-queries && .venv/bin/python wa.py poll-next --group "Soccer Mates" >> data/send.log 2>&1
```

With Docker, use `exec -T` (no TTY) for the same command:

```cron
0 18 * * 4 cd /opt/docker/soccer-training-queries && docker compose exec -T wa python wa.py poll-next --group "Soccer Mates" >> data/send.log 2>&1
```

If the daemon is down or disconnected, the command exits non-zero and writes
the error to stderr, so cron output/email is useful.

## Status and shutdown

```bash
.venv/bin/python wa.py status
```

Stop the daemon with `Ctrl+C` (foreground) or:

```bash
pkill -f 'wa.py serve'
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
uvx ruff check . --exclude .venv --exclude data
```

Ruff configuration lives in `ruff.toml`.

## Troubleshooting

- **QR expired / daemon never connects** — restart `wa.py serve` and scan the
  refreshed `data/qr.png`.
- **`session was logged out`** — the phone removed the linked device. Delete
  `data/wa_session.db` and pair again.
- **`group ... not found`** — run `wa.py groups` and use the exact name or JID.
- **`cannot reach daemon`** — start `wa.py serve` first; check `data/daemon.log`.
- **`not connected after 30s`** — check the daemon log and whether the phone
  still has an internet connection.
