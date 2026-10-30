"""Public iCalendar feeds and calendar-driven WhatsApp scheduling."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import icalendar
import recurring_ical_events
import requests

log = logging.getLogger("wa.calendar")

CACHE_SECONDS = 300
LOOKAHEAD_DAYS = 35
GRACE_MINUTES = 60
STATE_RETENTION_DAYS = 90

_OFFSET_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*"
    r"(m|min|mins|minute|minutes|h|std|stunde|stunden|d|tag|tage|days)?\s*$",
    re.IGNORECASE,
)


class CalendarError(Exception):
    """Raised when calendar data or configuration is invalid."""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise CalendarError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CalendarError(f"invalid JSON in {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_calendar_config(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    calendars = data.get("calendars")
    rules = data.get("rules")
    data["calendars"] = calendars if isinstance(calendars, list) else []
    data["rules"] = rules if isinstance(rules, list) else []
    return data


def save_calendar_config(path: Path, config: dict[str, Any]) -> None:
    _save_json(path, config)


def load_calendar_state(path: Path) -> dict[str, Any]:
    data = _load_json(path)
    if not isinstance(data.get("sent"), dict):
        data["sent"] = {}
    if not isinstance(data.get("rotations"), dict):
        data["rotations"] = {}
    return data


def save_calendar_state(path: Path, state: dict[str, Any]) -> None:
    _save_json(path, state)


def calendar_rules_enabled(path: Path) -> bool:
    try:
        config = load_calendar_config(path)
    except CalendarError:
        return False
    calendar_ids = {
        calendar.get("id")
        for calendar in config["calendars"]
        if calendar.get("enabled", True)
    }
    return any(
        rule.get("enabled", True)
        and rule.get("calendar_id") in calendar_ids
        for rule in config["rules"]
    )


def parse_offset(value: str) -> int:
    """Parse an offset like ``20h``, ``2d`` or ``90m`` into minutes."""
    match = _OFFSET_RE.match(str(value or ""))
    if not match:
        raise CalendarError(
            f"invalid offset {value!r}; use e.g. 20h, 2d or 90m"
        )
    amount = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "m").lower()
    if unit.startswith("d") or unit.startswith("tag"):
        minutes = amount * 1440
    elif unit.startswith("h") or unit.startswith("st"):
        minutes = amount * 60
    else:
        minutes = amount
    return int(round(minutes))


def match_event(rule: dict[str, Any], summary: str) -> bool:
    pattern = str(rule.get("match", "")).strip()
    if not pattern:
        return False
    mode = rule.get("match_type", "contains")
    if mode == "exact":
        return summary.strip().casefold() == pattern.casefold()
    if mode == "regex":
        try:
            return re.search(pattern, summary, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.casefold() in summary.casefold()


def fetch_ics(url: str, timeout: float = 15.0) -> bytes:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "whatsapp-agent-calendar/1.0"},
    )
    response.raise_for_status()
    return response.content


def expand_events(
    ics_bytes: bytes, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    try:
        calendar = icalendar.Calendar.from_ical(ics_bytes)
    except Exception as exc:
        raise CalendarError(f"cannot parse iCalendar data: {exc}") from exc

    try:
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except Exception as exc:
        raise CalendarError(f"cannot expand calendar: {exc}") from exc

    events: list[dict[str, Any]] = []
    for event in occurrences:
        status = str(event.get("STATUS", "")).strip().upper()
        if status == "CANCELLED":
            continue
        dtstart = event.get("DTSTART")
        if dtstart is None:
            continue
        value = dtstart.dt
        if isinstance(value, datetime):
            start_dt = value
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        elif isinstance(value, date):
            start_dt = datetime(
                value.year, value.month, value.day, tzinfo=timezone.utc
            )
        else:
            continue
        recurrence_id = event.get("RECURRENCE-ID")
        events.append(
            {
                "uid": str(event.get("UID", "")),
                "recurrence_id": (
                    recurrence_id.dt.isoformat()
                    if recurrence_id is not None
                    else ""
                ),
                "summary": str(event.get("SUMMARY", "")).strip(),
                "start": start_dt,
            }
        )
    events.sort(key=lambda item: item["start"])
    return events


class CalendarFeedCache:
    def __init__(self, ttl: float = CACHE_SECONDS) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, url: str, force: bool = False) -> bytes:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(url)
            if entry is not None and not force and now - entry[0] < self.ttl:
                return entry[1]
        data = fetch_ics(url)
        with self._lock:
            self._data[url] = (now, data)
        return data


def _occurrence_key(rule: dict[str, Any], event: dict[str, Any]) -> str:
    identity = event["recurrence_id"] or event["start"].isoformat()
    return f"{rule.get('id', '')}|{event['uid']}|{identity}"


class CalendarScheduler:
    """Send WhatsApp messages for calendar events at a configured offset."""

    def __init__(
        self,
        service: Any,
        config_path: Path,
        state_path: Path,
        polls_path: Path,
        parse_polls: Callable[[str], list[dict[str, Any]]],
        interval: float = 20.0,
    ) -> None:
        self.service = service
        self.config_path = config_path
        self.state_path = state_path
        self.polls_path = polls_path
        self.parse_polls = parse_polls
        self.interval = interval
        self.cache = CalendarFeedCache()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="wa-calendar", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                log.exception("calendar tick failed")

    def _polls(self) -> list[dict[str, Any]]:
        try:
            text = self.polls_path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            return self.parse_polls(text)
        except Exception:
            return []

    def _events(
        self,
        calendar: dict[str, Any],
        start: datetime,
        end: datetime,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        data = self.cache.get(str(calendar.get("url", "")), force=force)
        return expand_events(data, start, end)

    def _send(
        self, rule: dict[str, Any], event: dict[str, Any], state: dict[str, Any]
    ) -> None:
        group = str(rule.get("group", "")).strip()
        if not group:
            raise CalendarError("rule has no group")
        if rule.get("kind", "poll") == "text":
            text = str(rule.get("text", "")).strip()
            if not text:
                raise CalendarError("text rule has no text")
            self.service.send(group, text)
            return

        polls = self._polls()
        by_question = {poll["question"]: poll for poll in polls}
        rotation = [
            question
            for question in (rule.get("poll_questions") or [])
            if question in by_question
        ]
        if not rotation:
            rotation = [poll["question"] for poll in polls]
        if not rotation:
            raise CalendarError("no polls configured in polls.txt")
        index = int(state["rotations"].get(rule.get("id", ""), 0))
        question = rotation[index % len(rotation)]
        poll = by_question[question]
        self.service.poll(group, poll["question"], poll["options"])
        state["rotations"][rule.get("id", "")] = (index + 1) % len(rotation)

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Process due events once. Returns the sent entries (for tests)."""
        config = load_calendar_config(self.config_path)
        calendars = [
            calendar
            for calendar in config["calendars"]
            if calendar.get("enabled", True)
        ]
        rules = [rule for rule in config["rules"] if rule.get("enabled", True)]
        if not calendars or not rules:
            return []

        moment = now or datetime.now(timezone.utc)
        window_start = moment - timedelta(minutes=GRACE_MINUTES)
        window_end = moment + timedelta(days=LOOKAHEAD_DAYS)
        state = load_calendar_state(self.state_path)
        sent_now: list[dict[str, Any]] = []
        changed = False

        for calendar in calendars:
            calendar_rules = [
                rule
                for rule in rules
                if rule.get("calendar_id") == calendar.get("id")
            ]
            if not calendar_rules:
                continue
            try:
                events = self._events(calendar, window_start, window_end)
            except Exception as exc:
                log.warning(
                    "could not load calendar %s: %s",
                    calendar.get("name") or calendar.get("url"),
                    exc,
                )
                continue

            due: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            for rule in calendar_rules:
                offset = parse_offset(rule.get("offset", "20h"))
                for event in events:
                    if not match_event(rule, event["summary"]):
                        continue
                    due_at = event["start"] - timedelta(minutes=offset)
                    if due_at > moment:
                        continue
                    if moment - due_at > timedelta(minutes=GRACE_MINUTES):
                        continue
                    key = _occurrence_key(rule, event)
                    if key in state["sent"]:
                        continue
                    due.append((rule, event, key))

            if not due:
                continue

            # Refresh once so a deleted single occurrence is respected.
            try:
                fresh = self._events(
                    calendar, window_start, window_end, force=True
                )
            except Exception:
                fresh = events
            fresh_keys = {
                _occurrence_key(rule, event): event
                for rule in calendar_rules
                for event in fresh
                if match_event(rule, event["summary"])
            }

            for rule, event, key in due:
                if key not in fresh_keys:
                    state["sent"][key] = int(moment.timestamp())
                    changed = True
                    log.info(
                        "calendar occurrence %s for %r was removed; skipping",
                        event["uid"],
                        event["summary"],
                    )
                    continue
                if not self.service.connected.is_set():
                    log.warning(
                        "calendar message for %r is due but WhatsApp is "
                        "not connected; retrying",
                        event["summary"],
                    )
                    continue
                try:
                    self._send(rule, fresh_keys[key], state)
                except Exception:
                    log.exception(
                        "calendar send failed for %r", event["summary"]
                    )
                    continue
                state["sent"][key] = int(moment.timestamp())
                changed = True
                sent_now.append(
                    {
                        "rule": rule,
                        "event": fresh_keys[key],
                        "sent_at": moment,
                    }
                )

        if changed:
            self._prune(state, moment)
            save_calendar_state(self.state_path, state)
        return sent_now

    def _prune(self, state: dict[str, Any], now: datetime) -> None:
        cutoff = (now - timedelta(days=STATE_RETENTION_DAYS)).timestamp()
        state["sent"] = {
            key: value
            for key, value in state["sent"].items()
            if isinstance(value, (int, float)) and value >= cutoff
        }

    def preview(
        self, now: datetime | None = None, limit: int = 12
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return upcoming sends (with rotation preview) and an error string."""
        config = load_calendar_config(self.config_path)
        calendars = [
            calendar
            for calendar in config["calendars"]
            if calendar.get("enabled", True)
        ]
        rules = [rule for rule in config["rules"] if rule.get("enabled", True)]
        moment = now or datetime.now(timezone.utc)
        window_start = moment - timedelta(minutes=GRACE_MINUTES)
        window_end = moment + timedelta(days=LOOKAHEAD_DAYS)
        polls = self._polls()
        by_question = {poll["question"]: poll for poll in polls}
        rotations = dict(load_calendar_state(self.state_path)["rotations"])
        items: list[dict[str, Any]] = []
        errors: list[str] = []

        for calendar in calendars:
            calendar_rules = [
                rule
                for rule in rules
                if rule.get("calendar_id") == calendar.get("id")
            ]
            if not calendar_rules:
                continue
            try:
                events = self._events(calendar, window_start, window_end)
            except Exception as exc:
                errors.append(
                    f"{calendar.get('name') or calendar.get('url')}: {exc}"
                )
                continue
            for rule in calendar_rules:
                offset = parse_offset(rule.get("offset", "20h"))
                for event in events:
                    if not match_event(rule, event["summary"]):
                        continue
                    send_at = event["start"] - timedelta(minutes=offset)
                    if send_at < moment - timedelta(minutes=GRACE_MINUTES):
                        continue
                    kind = rule.get("kind", "poll")
                    if kind == "text":
                        content = str(rule.get("text", "")).strip()
                    else:
                        rotation = [
                            question
                            for question in (rule.get("poll_questions") or [])
                            if question in by_question
                        ]
                        if not rotation:
                            rotation = [poll["question"] for poll in polls]
                        if not rotation:
                            content = "(keine Umfrage in polls.txt)"
                        else:
                            index = int(rotations.get(rule.get("id", ""), 0))
                            content = rotation[index % len(rotation)]
                            rotations[rule.get("id", "")] = index + 1
                    items.append(
                        {
                            "send_at": send_at,
                            "start": event["start"],
                            "summary": event["summary"],
                            "kind": kind,
                            "content": content,
                            "group": rule.get("group", ""),
                        }
                    )

        items.sort(key=lambda item: item["send_at"])
        error = "; ".join(errors) if errors else None
        return items[:limit], error
