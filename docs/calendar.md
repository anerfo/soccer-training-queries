# Calendar-driven messages

Add a calendar with its public ICS URL. In Google Calendar open
*Settings → Integrate calendar* and use the **Secret address in iCal format**
or the public `…/public/basic.ics` link. No OAuth is required; only public
feeds are read.

## Rules

Rules match events by title (`contains`, `exact` or `regex`) and define:

- the target group,
- the message type: a poll from `polls.txt` or a free text message,
- the offset before the event (for example `20h`, `2d`, `90m`),
- an optional **rotation list** for polls (one question per line, in order;
  empty means rotate through all questions in `polls.txt`).

Recurring events are expanded with their `RRULE`; deleting a single occurrence
in Google Calendar (or marking it cancelled) removes it from the feed, so no
message is sent for it. The feed is re-fetched right before sending to honour
last-minute deletions.

## Preview and calendar view

The **Vorschau** table on the calendar page shows the upcoming sends and the
poll rotation. The monthly **Kalenderansicht** is embedded on the
**Übersicht** (`/#calendar`); it shows calendar events and planned/sent
messages, and clicking an entry opens a detail dialog.

Calendar configuration lives in `data/calendars.json`; send state (including
sent-message history) lives in `data/calendar_state.json`.

## Local feeds

Calendar fetching blocks private, loopback, link-local and metadata addresses
(SSRF protection). Set `CALENDAR_ALLOW_PRIVATE=1` only if you intentionally use
a local ICS feed.

## Scheduler behaviour

- A message is sent at `event start - offset`.
- Each occurrence is sent at most once.
- If the daemon is offline or WhatsApp is disconnected at the due time, sending
  is retried for up to 60 minutes.
- An invalid rule (for example an unparsable offset) is skipped and reported in
  the preview instead of stopping the scheduler.
