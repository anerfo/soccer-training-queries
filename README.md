# whatsapp-agent

A small Python daemon and CLI for sending messages to a WhatsApp group (for
example, a soccer group). It uses [neonize](https://github.com/krypton-byte/neonize)
(whatsmeow) and your own WhatsApp account.

> The official WhatsApp Cloud API cannot send to groups, so this project uses
> the unofficial multi-device protocol. Automation can get a number banned.
> Prefer a secondary number for the bot.

## How it works

- `wa.py serve` keeps the WhatsApp session connected, prints a QR code for
  pairing, and listens on a local Unix socket (`data/wa.sock`).
- `wa.py send ...` asks the daemon to send a message. This is what cron calls.
- The session is stored in `data/wa_session.db`, so you only scan the QR once.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

FFmpeg is optional; it is only needed for media messages.

## First run (pair the account)

```bash
.venv/bin/python wa.py serve
```

A QR code is printed in the terminal and saved to `data/qr.png`. On your phone
open **WhatsApp > Linked devices > Link a device** and scan it. The image file
is overwritten as the QR rotates.

Once you see `Connected to WhatsApp`, leave the daemon running.

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
export WA_SOCKET="/home/andi/code/whatsapp-agent/data/wa.sock"
```

## Recurring training questionnaire

Send a native WhatsApp poll asking who is coming to training:

```bash
.venv/bin/python wa.py poll --group "Soccer Mates" \
  --question "Training this week - are you joining?" \
  --option "Yes" --option "No" --option "Maybe"
```

If you omit `--option`, the poll uses `Yes`, `No`, `Maybe`. Add `--multiple`
to let people pick more than one answer.

The daemon is send-only, so it does not read the votes. WhatsApp displays the
results and voters inside the poll itself.

## Cron

Cron only needs the `send` or `poll` command; the daemon must already be
running.

```cron
# Every Saturday at 10:00, remind the group about the match.
0 10 * * 6 cd /home/andi/code/whatsapp-agent && .venv/bin/python wa.py send --group "Soccer Mates" --text "Match today at 7pm!" >> data/send.log 2>&1

# Every Thursday at 18:00, ask who is joining training.
0 18 * * 4 cd /home/andi/code/whatsapp-agent && .venv/bin/python wa.py poll --group "Soccer Mates" --question "Training this week - are you joining?" >> data/send.log 2>&1
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

## Troubleshooting

- **QR expired / daemon never connects** — restart `wa.py serve` and scan the
  refreshed `data/qr.png`.
- **`session was logged out`** — the phone removed the linked device. Delete
  `data/wa_session.db` and pair again.
- **`group ... not found`** — run `wa.py groups` and use the exact name or JID.
- **`cannot reach daemon`** — start `wa.py serve` first; check `data/daemon.log`.
- **`not connected after 30s`** — check the daemon log and whether the phone
  still has an internet connection.
