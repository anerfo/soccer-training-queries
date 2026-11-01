# Polls, results & contacts

## Poll list

The recurring polls live in `polls.txt`, one poll per line:

```text
# Frage | Antwort 1 | Antwort 2
AH-Training am Mittwoch – bist du dabei? | Ich bin am Start | Ich bin raus
🏟️ „Und da ist er wieder, der AH-Kicker!“ – bist du dabei? | Da bin ich | Da bin ich nicht
```

Blank lines and lines starting with `#` are ignored. The file can be edited in
the web UI or directly; the daemon does not need a restart.

The bot walks through the list in order and wraps around, so consecutive sends
get a different message:

```bash
# Send the next poll from polls.txt
.venv/bin/python wa.py poll-next --group "Soccer Mates"

# Preview the list and see which one is next
.venv/bin/python wa.py polls

# Check the next choice without sending or advancing the rotation
.venv/bin/python wa.py poll-next --group "Soccer Mates" --dry-run
```

The rotation is stored in `data/polls_state.json`. Delete that file to start
again from the first line. Add `--random` to `poll-next` to pick a random poll
that is never the same as the previous one.

The dashboard's **Jetzt senden** first uses the next planned calendar
message. If no calendar message is planned, it falls back to the next poll
from `polls.txt` (manual rotation). One-off polls are also possible:

```bash
.venv/bin/python wa.py poll --group "Soccer Mates" \
  --question "AH-Training am Mittwoch – bist du dabei?" \
  --option "Ich bin am Start" --option "Ich bin raus"
```

## Results

The daemon records every poll it sends together with the votes it receives
(`data/poll_history.json`, last 50 polls). The **Ergebnisse** page lets you:

- step through the most recent polls (newer/older),
- see per-option totals and who voted for what,
- click an answer to filter the table and the Excel export,
- open **Namen** to map phone numbers to names,
- copy or download just the names (one per line, no header) for the current
  filter directly into Excel.

Votes are read from WhatsApp's poll update messages, so the daemon must be
running and connected when the group votes. Votes cast on a poll that was sent
before the daemon stored it, or with a session/database that no longer holds the
poll's message secret, cannot be decrypted and are skipped.

## Contacts

The **Namen** page maps phone numbers to names (`data/contacts.json`). Numbers
and names are prefilled from the configured group and the WhatsApp contact
store; the WhatsApp display name (push name) is shown under each number. The
list can be filtered and edited per row, and new numbers can be added manually.
