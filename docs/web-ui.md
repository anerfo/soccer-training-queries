# Web UI

`wa.py serve` starts the web UI on `http://127.0.0.1:8080` by default. Use
`--no-web` to disable it, or `--web-host`/`--web-port` to change the bind
address.

The UI has these pages:

- **Übersicht** — connection status, a preview of the next poll, calendar
  counts and a "send now" button.
- **Umfragen** — edit `polls.txt` in the browser. The list is validated before
  saving and a preview is shown below the editor.
- **Ergebnisse** — browse the most recently sent polls, see who accepted and
  declined, filter by answer and export results. See [Polls & results](polls.md).
- **Namen** — map phone numbers to names in an editable table. Numbers and
  names are filled automatically from the configured group and the WhatsApp
  contact store (the phone's address book); missing names can be typed in.
  The WhatsApp display name (push name) is shown under each number as a hint.
  The mapping lives in `data/contacts.json`.
- **Kalender** — attach public Google Calendar ICS feeds and send WhatsApp
  messages/polls relative to events. See [Calendar](calendar.md).

The UI works on mobile too; the navigation collapses into a burger menu.

## Simulation data

While WhatsApp is not connected, the dashboard shows a **Simulationsdaten
hinzufügen** button below the QR code. It creates a test poll (using a question
from `polls.txt`) with random yes/no votes from made-up members, so the
**Ergebnisse** and **Namen** pages can be tried without a real account. A
**Simulationsdaten löschen** button removes those test polls again; real polls
are kept.
