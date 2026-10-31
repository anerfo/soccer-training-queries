"""Calendar-driven WhatsApp scheduling."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .data import (
    DEFAULT_CALENDAR_TIMEZONE,
    ERROR_CACHE_SECONDS,
    GRACE_MINUTES,
    LOOKAHEAD_DAYS,
    STATE_RETENTION_DAYS,
    CalendarError,
    CalendarFeedCache,
    _occurrence_key,
    expand_events,
    load_calendar_config,
    load_calendar_state,
    match_event,
    parse_offset,
    save_calendar_state,
)

log = logging.getLogger("wa.calendar")


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
        self._error_logged: dict[str, float] = {}
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
    ) -> dict[str, Any]:
        group = str(rule.get("group", "")).strip()
        if not group:
            raise CalendarError("rule has no group")
        if rule.get("kind", "poll") == "text":
            text = str(rule.get("text", "")).strip()
            if not text:
                raise CalendarError("text rule has no text")
            self.service.send(group, text)
            return {"kind": "text", "content": text, "group": group}

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
        return {
            "kind": "poll",
            "content": question,
            "group": group,
            "question": question,
        }

    def _log_calendar_error(self, calendar: dict[str, Any], exc: Exception) -> None:
        url = str(calendar.get("url", ""))
        now_ts = time.monotonic()
        if now_ts - self._error_logged.get(url, 0) >= ERROR_CACHE_SECONDS:
            log.warning(
                "could not load calendar %s: %s",
                calendar.get("name") or "calendar",
                str(exc)[:200],
            )
            self._error_logged[url] = now_ts

    def _log_rule_error(self, rule: dict[str, Any], exc: Exception) -> None:
        key = f"rule:{rule.get('id', '')}"
        now_ts = time.monotonic()
        if now_ts - self._error_logged.get(key, 0) >= ERROR_CACHE_SECONDS:
            log.warning("skipping calendar rule %s: %s", rule.get("id"), exc)
            self._error_logged[key] = now_ts

    def _due_entries(
        self,
        calendar_rules: list[dict[str, Any]],
        events: list[dict[str, Any]],
        moment: datetime,
        sent: dict[str, Any],
    ) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
        due: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for rule in calendar_rules:
            try:
                offset = parse_offset(rule.get("offset", "20h"))
            except CalendarError as exc:
                self._log_rule_error(rule, exc)
                continue
            for event in events:
                if not match_event(rule, event["summary"]):
                    continue
                due_at = event["start"] - timedelta(minutes=offset)
                if due_at > moment:
                    continue
                if moment - due_at > timedelta(minutes=GRACE_MINUTES):
                    continue
                key = _occurrence_key(rule, event)
                if key in sent:
                    continue
                due.append((rule, event, key))
        return due

    @staticmethod
    def _fresh_index(
        calendar_rules: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        return {
            _occurrence_key(rule, event): event
            for rule in calendar_rules
            for event in fresh
            if match_event(rule, event["summary"])
        }

    def _send_due(
        self,
        due: list[tuple[dict[str, Any], dict[str, Any], str]],
        fresh_keys: dict[str, dict[str, Any]],
        moment: datetime,
        state: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        changed = False
        sent_now: list[dict[str, Any]] = []
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
                    "calendar message for %r is due but WhatsApp is not "
                    "connected; retrying",
                    event["summary"],
                )
                continue
            try:
                info = self._send(rule, fresh_keys[key], state)
            except Exception:
                log.exception("calendar send failed for %r", event["summary"])
                continue
            timestamp = int(moment.timestamp())
            state["sent"][key] = timestamp
            state.setdefault("history", {})[key] = {
                "sent_at": timestamp,
                **info,
            }
            changed = True
            sent_now.append({"rule": rule, "event": fresh_keys[key], "sent_at": moment})
        return changed, sent_now

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

        moment = now or datetime.now(UTC)
        window_start = moment - timedelta(minutes=GRACE_MINUTES)
        window_end = moment + timedelta(days=LOOKAHEAD_DAYS)
        state = load_calendar_state(self.state_path)
        changed = False
        sent_now: list[dict[str, Any]] = []

        for calendar in calendars:
            calendar_rules = [
                rule for rule in rules if rule.get("calendar_id") == calendar.get("id")
            ]
            if not calendar_rules:
                continue
            try:
                events = self._events(calendar, window_start, window_end)
            except Exception as exc:
                self._log_calendar_error(calendar, exc)
                continue
            self._error_logged.pop(str(calendar.get("url", "")), None)

            due = self._due_entries(calendar_rules, events, moment, state["sent"])
            if not due:
                continue

            try:
                fresh = self._events(calendar, window_start, window_end, force=True)
            except Exception:
                fresh = events
            fresh_keys = self._fresh_index(calendar_rules, fresh)
            entry_changed, entry_sent = self._send_due(due, fresh_keys, moment, state)
            changed = changed or entry_changed
            sent_now.extend(entry_sent)

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
        history = state.get("history")
        if isinstance(history, dict):
            state["history"] = {
                key: value
                for key, value in history.items()
                if isinstance(value.get("sent_at"), (int, float))
                and value["sent_at"] >= cutoff
            }

    @staticmethod
    def _max_offset(rules: list[dict[str, Any]]) -> int:
        max_offset = 0
        for rule in rules:
            try:
                max_offset = max(max_offset, parse_offset(rule.get("offset", "20h")))
            except CalendarError:
                continue
        return max_offset

    @staticmethod
    def _event_entry(
        calendar: dict[str, Any],
        event: dict[str, Any],
        start: datetime,
        end: datetime | None,
        matching: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "calendar": calendar.get("name", ""),
            "summary": event["summary"],
            "start": start,
            "end": end,
            "all_day": event.get("all_day", False),
            "location": event.get("location", ""),
            "description": event.get("description", ""),
            "uid": event["uid"],
            "recurrence_id": event["recurrence_id"],
            "rules": [rule.get("match", "") for rule in matching],
        }

    @staticmethod
    def _send_item(
        calendar: dict[str, Any],
        event: dict[str, Any],
        start: datetime,
        rule: dict[str, Any],
        offset: int,
        key: str,
    ) -> dict[str, Any]:
        kind = rule.get("kind", "poll")
        return {
            "send_at": start - timedelta(minutes=offset),
            "event_start": start,
            "summary": event["summary"],
            "calendar": calendar.get("name", ""),
            "group": rule.get("group", ""),
            "kind": kind,
            "content": str(rule.get("text", "")) if kind == "text" else "",
            "rule_match": rule.get("match", ""),
            "key": key,
            "uid": event["uid"],
            "recurrence_id": event["recurrence_id"],
            "sent": False,
        }

    @staticmethod
    def _rotation_candidates(
        rule: dict[str, Any],
        by_question: dict[str, dict[str, Any]],
        polls: list[dict[str, Any]],
    ) -> list[str]:
        rotation = [
            question
            for question in (rule.get("poll_questions") or [])
            if question in by_question
        ]
        return rotation or [poll["question"] for poll in polls]

    def _append_sends(
        self,
        rule_items: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
        by_question: dict[str, dict[str, Any]],
        polls: list[dict[str, Any]],
        rotations: dict[str, Any],
        history: dict[str, Any],
        sent_keys: dict[str, Any],
        moment: datetime,
        sends_out: list[dict[str, Any]],
    ) -> None:
        for rule_id, entries in rule_items.items():
            rule = entries[0][1]
            rotation = self._rotation_candidates(rule, by_question, polls)
            for item, _rule in sorted(entries, key=lambda pair: pair[0]["send_at"]):
                key = item["key"]
                if key in history:
                    stored = history[key]
                    item["kind"] = stored.get("kind", item["kind"])
                    item["content"] = stored.get("content", "")
                    item["group"] = stored.get("group", item["group"])
                    item["sent"] = True
                elif item["send_at"] >= moment and rotation:
                    index = int(rotations.get(rule_id, 0))
                    item["content"] = rotation[index % len(rotation)]
                    rotations[rule_id] = index + 1
                else:
                    item["sent"] = key in sent_keys
                if item["kind"] == "poll" and not item["content"]:
                    item["content"] = "(keine Umfrage in polls.txt)"
                sends_out.append(item)

    def _collect_calendar(
        self,
        calendar: dict[str, Any],
        calendar_rules: list[dict[str, Any]],
        events: list[dict[str, Any]],
        start_local: datetime,
        end_local: datetime,
        tz: ZoneInfo,
        include_events: bool,
        events_out: list[dict[str, Any]],
        errors: list[str],
    ) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
        rule_items: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for event in events:
            start = event["start"].astimezone(tz)
            end = event["end"].astimezone(tz) if event.get("end") else None
            matching = [
                rule for rule in calendar_rules if match_event(rule, event["summary"])
            ]
            if include_events and start_local <= start < end_local:
                events_out.append(
                    self._event_entry(calendar, event, start, end, matching)
                )
            for rule in matching:
                try:
                    offset = parse_offset(rule.get("offset", "20h"))
                except CalendarError as exc:
                    errors.append(f"rule {rule.get('match', '')!r}: {exc}")
                    continue
                send_at = start - timedelta(minutes=offset)
                if not (start_local <= send_at < end_local):
                    continue
                key = _occurrence_key(rule, event)
                rule_items.setdefault(rule.get("id", ""), []).append(
                    (
                        self._send_item(calendar, event, start, rule, offset, key),
                        rule,
                    )
                )
        return rule_items

    def _collect_window(
        self,
        *,
        start_local: datetime,
        end_local: datetime,
        moment: datetime,
        timezone_name: str,
        include_events: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        tz = ZoneInfo(timezone_name)
        config = load_calendar_config(self.config_path)
        calendars = [
            calendar
            for calendar in config["calendars"]
            if calendar.get("enabled", True)
        ]
        rules = [rule for rule in config["rules"] if rule.get("enabled", True)]
        state = load_calendar_state(self.state_path)
        history = state.get("history", {})
        sent_keys = state.get("sent", {})
        rotations = dict(state.get("rotations", {}))
        polls = self._polls()
        by_question = {poll["question"]: poll for poll in polls}
        max_offset = self._max_offset(rules)
        window_start = start_local - timedelta(days=2)
        window_end = end_local + timedelta(minutes=max_offset + 1440)

        events_out: list[dict[str, Any]] = []
        sends_out: list[dict[str, Any]] = []
        errors: list[str] = []

        for calendar in calendars:
            calendar_rules = [
                rule for rule in rules if rule.get("calendar_id") == calendar.get("id")
            ]
            if not calendar_rules:
                continue
            try:
                events = self._events(calendar, window_start, window_end)
            except Exception as exc:
                errors.append(f"{calendar.get('name') or 'calendar'}: {exc}")
                continue
            rule_items = self._collect_calendar(
                calendar,
                calendar_rules,
                events,
                start_local,
                end_local,
                tz,
                include_events,
                events_out,
                errors,
            )
            self._append_sends(
                rule_items,
                by_question,
                polls,
                rotations,
                history,
                sent_keys,
                moment,
                sends_out,
            )

        events_out.sort(key=lambda item: item["start"])
        sends_out.sort(key=lambda item: item["send_at"])
        return events_out, sends_out, errors

    def preview(
        self, now: datetime | None = None, limit: int = 12
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return upcoming sends (with rotation preview) and an error string."""
        moment = now or datetime.now(UTC)
        _events, sends, errors = self._collect_window(
            start_local=moment - timedelta(minutes=GRACE_MINUTES),
            end_local=moment + timedelta(days=LOOKAHEAD_DAYS),
            moment=moment,
            timezone_name=DEFAULT_CALENDAR_TIMEZONE,
            include_events=False,
        )
        items = [
            {
                "send_at": send["send_at"],
                "start": send["event_start"],
                "summary": send["summary"],
                "kind": send["kind"],
                "content": send["content"],
                "group": send["group"],
            }
            for send in sends[:limit]
        ]
        return items, ("; ".join(errors) if errors else None)

    def month_entries(
        self,
        year: int,
        month: int,
        timezone_name: str = DEFAULT_CALENDAR_TIMEZONE,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Return calendar events and planned/sent messages for a month."""
        start_local = datetime(year, month, 1, tzinfo=ZoneInfo(timezone_name))
        if month == 12:
            next_local = datetime(year + 1, 1, 1, tzinfo=ZoneInfo(timezone_name))
        else:
            next_local = datetime(year, month + 1, 1, tzinfo=ZoneInfo(timezone_name))
        moment = now or datetime.now(UTC)
        events, sends, errors = self._collect_window(
            start_local=start_local,
            end_local=next_local,
            moment=moment,
            timezone_name=timezone_name,
            include_events=True,
        )
        return (
            {"events": events, "sends": sends},
            "; ".join(errors) if errors else None,
        )
