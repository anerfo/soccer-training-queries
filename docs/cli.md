# CLI & cron

All commands talk to the running daemon over the local Unix socket
(`data/wa.sock`), so start `wa.py serve` first.

## Send a message

```bash
.venv/bin/python wa.py send --group "Soccer Mates" --text "Match at 7pm"
```

Check the group resolution without sending:

```bash
.venv/bin/python wa.py send --group "Soccer Mates" --text "test" --dry-run
```

Pipe text or read a file:

```bash
echo "Match at 7pm" | .venv/bin/python wa.py send --group "Soccer Mates"
.venv/bin/python wa.py send --group "Soccer Mates" --text-file reminder.txt
```

Defaults can come from the environment:

```bash
export WA_GROUP="Soccer Mates"
export WA_SOCKET="/opt/docker/soccer-training-queries/data/wa.sock"
```

## Polls

```bash
.venv/bin/python wa.py poll-next --group "Soccer Mates"
.venv/bin/python wa.py polls
.venv/bin/python wa.py poll-next --group "Soccer Mates" --dry-run
```

Add `--random` to avoid repeating the previous poll. See
[Polls & results](polls.md) for the file format and rotation state.

## Groups and status

```bash
.venv/bin/python wa.py groups
.venv/bin/python wa.py status
.venv/bin/python wa.py groups --json
```

## Cron

The **Kalender** page is the recommended way to schedule recurring polls,
reports and reminders. Cron is still useful for one-off sends; it only needs
the `send` or `poll-next` command and the daemon must already be running.

```cron
# Every Saturday at 10:00, remind the group about the match.
0 10 * * 6 cd /opt/docker/soccer-training-queries && .venv/bin/python wa.py send --group "Soccer Mates" --text "Match today at 7pm!" >> data/send.log 2>&1

# Every Thursday at 18:00, send the next training poll.
0 18 * * 4 cd /opt/docker/soccer-training-queries && .venv/bin/python wa.py poll-next --group "Soccer Mates" >> data/send.log 2>&1
```

With Docker, use `exec -T` (no TTY):

```cron
0 18 * * 4 cd /opt/docker/soccer-training-queries && docker compose exec -T wa python wa.py poll-next --group "Soccer Mates" >> data/send.log 2>&1
```

If the daemon is down or disconnected, the command exits non-zero and writes
the error to stderr, so cron output/email is useful.

## Shutdown

Stop the daemon with `Ctrl+C` (foreground) or:

```bash
pkill -f 'wa.py serve'
```
