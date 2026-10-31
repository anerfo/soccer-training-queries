"""Public iCalendar feeds, configuration, caching and event expansion."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import icalendar
import recurring_ical_events
import requests

CACHE_SECONDS = 300
ERROR_CACHE_SECONDS = 300
LOOKAHEAD_DAYS = 35
GRACE_MINUTES = 60
STATE_RETENTION_DAYS = 90
DEFAULT_CALENDAR_TIMEZONE = "Europe/Berlin"

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
    if not isinstance(data.get("history"), dict):
        data["history"] = {}
    return data


def save_calendar_state(path: Path, state: dict[str, Any]) -> None:
    _save_json(path, state)


def parse_offset(value: str) -> int:
    """Parse an offset like ``20h``, ``2d`` or ``90m`` into minutes."""
    match = _OFFSET_RE.match(str(value or ""))
    if not match:
        raise CalendarError(f"invalid offset {value!r}; use e.g. 20h, 2d or 90m")
    amount = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "m").lower()
    if unit.startswith(("d", "tag")):
        minutes = amount * 1440
    elif unit.startswith(("h", "st")):
        minutes = amount * 60
    else:
        minutes = amount
    return round(minutes)


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
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "whatsapp-agent-calendar/1.0",
                "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.5",
            },
        )
    except Exception as exc:
        raise CalendarError(f"could not fetch calendar: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise CalendarError(f"calendar URL returned HTTP {response.status_code}")
    body = response.content
    if b"begin:vcalendar" not in body[:1024].lower():
        raise CalendarError(
            "URL did not return iCalendar data; use the public or secret "
            "Google Calendar address ending in /basic.ics"
        )
    return body


def expand_events(
    ics_bytes: bytes, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    try:
        calendar = icalendar.Calendar.from_ical(ics_bytes)
    except Exception as exc:
        message = str(exc).splitlines()[0][:200]
        raise CalendarError(f"cannot parse iCalendar data: {message}") from exc

    try:
        occurrences = recurring_ical_events.of(calendar).between(start, end)
    except Exception as exc:
        raise CalendarError(f"cannot expand calendar: {exc}") from exc

    def as_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=UTC)
        return None

    events: list[dict[str, Any]] = []
    for event in occurrences:
        status = str(event.get("STATUS", "")).strip().upper()
        if status == "CANCELLED":
            continue
        dtstart = event.get("DTSTART")
        if dtstart is None:
            continue
        start_dt = as_datetime(dtstart.dt)
        if start_dt is None:
            continue
        dtend = event.get("DTEND")
        end_dt = as_datetime(dtend.dt) if dtend is not None else None
        recurrence_id = event.get("RECURRENCE-ID")
        events.append(
            {
                "uid": str(event.get("UID", "")),
                "recurrence_id": (
                    recurrence_id.dt.isoformat() if recurrence_id is not None else ""
                ),
                "summary": str(event.get("SUMMARY", "")).strip(),
                "start": start_dt,
                "end": end_dt,
                "all_day": not isinstance(dtstart.dt, datetime),
                "location": str(event.get("LOCATION", "")).strip(),
                "description": str(event.get("DESCRIPTION", "")).strip(),
            }
        )
    events.sort(key=lambda item: item["start"])
    return events


class CalendarFeedCache:
    def __init__(
        self,
        ttl: float = CACHE_SECONDS,
        error_ttl: float = ERROR_CACHE_SECONDS,
    ) -> None:
        self.ttl = ttl
        self.error_ttl = error_ttl
        self._data: dict[str, tuple[float, bytes]] = {}
        self._errors: dict[str, tuple[float, CalendarError]] = {}
        self._lock = threading.Lock()

    def get(self, url: str, force: bool = False) -> bytes:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(url)
            if entry is not None and not force and now - entry[0] < self.ttl:
                return entry[1]
            error = self._errors.get(url)
            if error is not None and not force and now - error[0] < self.error_ttl:
                raise error[1]
        try:
            data = fetch_ics(url)
        except CalendarError as exc:
            with self._lock:
                self._errors[url] = (now, exc)
            raise
        except Exception as exc:
            short = CalendarError(f"could not fetch calendar: {type(exc).__name__}")
            with self._lock:
                self._errors[url] = (now, short)
            raise short from exc
        with self._lock:
            self._data[url] = (now, data)
            self._errors.pop(url, None)
        return data


def _occurrence_key(rule: dict[str, Any], event: dict[str, Any]) -> str:
    identity = event["recurrence_id"] or event["start"].isoformat()
    return f"{rule.get('id', '')}|{event['uid']}|{identity}"
