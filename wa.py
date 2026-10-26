#!/usr/bin/env python3
"""Send WhatsApp group messages from a small local daemon.

The daemon owns the WhatsApp session (neonize/whatsmeow) and listens on a
Unix socket. The CLI can then be used from cron or by hand without keeping a
second WhatsApp connection open.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import socket
import socketserver
import sys
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_SOCKET = DEFAULT_DATA_DIR / "wa.sock"
DEFAULT_DB = DEFAULT_DATA_DIR / "wa_session.db"
DEFAULT_QR = DEFAULT_DATA_DIR / "qr.png"
DEFAULT_POLLS_FILE = BASE_DIR / "polls.txt"
DEFAULT_POLL_STATE = DEFAULT_DATA_DIR / "polls_state.json"
DEFAULT_SCHEDULE_FILE = DEFAULT_DATA_DIR / "schedule.json"
DEFAULT_SCHEDULE_STATE = DEFAULT_DATA_DIR / "schedule_state.json"

WEEKDAYS_DE = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
DEFAULT_TIMEZONE = "Europe/Berlin"
SCHEDULE_GRACE = timedelta(minutes=30)
SCHEDULER_INTERVAL = 20.0

CONNECT_WAIT = 30.0
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_MESSAGE_BYTES = 65_536
MAX_POLL_QUESTION = 255
MAX_POLL_OPTION = 100
MAX_POLL_OPTIONS = 12
DEFAULT_POLL_OPTIONS = ["Dabei", "Nicht dabei"]

log = logging.getLogger("wa")


def configure_logging(verbose: bool = False, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("neonize").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )


def normalize_text(text: str) -> str:
    if not text or not text.strip():
        raise ValueError("message text is empty")
    size = len(text.encode("utf-8"))
    if size > MAX_MESSAGE_BYTES:
        raise ValueError(
            f"message is too long ({size} bytes, max {MAX_MESSAGE_BYTES})"
        )
    return text


def normalize_poll(
    question: str, options: list[str]
) -> tuple[str, list[str]]:
    """Validate a poll and return the cleaned question and options."""
    question = question.strip()
    if not question:
        raise ValueError("poll question is empty")
    if len(question) > MAX_POLL_QUESTION:
        raise ValueError(
            f"poll question is too long (max {MAX_POLL_QUESTION} characters)"
        )

    cleaned = [option.strip() for option in options]
    if not 2 <= len(cleaned) <= MAX_POLL_OPTIONS:
        raise ValueError(f"a poll needs 2-{MAX_POLL_OPTIONS} options")
    if any(not option for option in cleaned):
        raise ValueError("poll options cannot be empty")
    if any(len(option) > MAX_POLL_OPTION for option in cleaned):
        raise ValueError(
            f"poll options must be at most {MAX_POLL_OPTION} characters"
        )
    if len({option.casefold() for option in cleaned}) != len(cleaned):
        raise ValueError("poll options must be unique")
    return question, cleaned


def parse_polls(text: str) -> list[dict[str, Any]]:
    """Parse the editable poll list.

    Format, one poll per line:
        Question | Answer 1 | Answer 2 | ...
    Blank lines and lines starting with # are ignored.
    """
    polls: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            raise ValueError(
                f"line {line_number}: expected "
                "'question | answer 1 | answer 2'"
            )
        question, options = normalize_poll(parts[0], parts[1:])
        polls.append({"question": question, "options": options})
    if not polls:
        raise ValueError("no polls found in the list")
    return polls


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"cannot read state file {path}: {exc}") from exc
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid state file {path}: {exc}") from exc
    return state if isinstance(state, dict) else {}


def _save_json_object(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_poll_state(path: Path) -> dict[str, Any]:
    return _load_json_object(path)


def save_poll_state(path: Path, state: dict[str, Any]) -> None:
    _save_json_object(path, state)


def load_schedule_state(path: Path) -> dict[str, Any]:
    return _load_json_object(path)


def save_schedule_state(path: Path, state: dict[str, Any]) -> None:
    _save_json_object(path, state)


def default_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "weekday": 2,
        "time": "18:00",
        "timezone": DEFAULT_TIMEZONE,
        "group": os.environ.get("WA_GROUP", ""),
        "skip_dates": [],
        "extra_dates": [],
    }


def _normalize_date_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f"{field} must be a list of dates")

    dates: set[str] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid date in {field}: {text}") from exc
        dates.add(parsed.isoformat())
    return sorted(dates)


def normalize_schedule(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("schedule must be a JSON object")

    try:
        weekday = int(data.get("weekday", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError("weekday must be a number from 0 to 6") from exc
    if not 0 <= weekday <= 6:
        raise ValueError("weekday must be a number from 0 to 6")

    time_text = str(data.get("time", "18:00")).strip()
    try:
        parsed_time = datetime.strptime(time_text, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("time must use the HH:MM format") from exc

    timezone_name = str(data.get("timezone", DEFAULT_TIMEZONE)).strip()
    if not timezone_name:
        timezone_name = DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc

    enabled_value = data.get("enabled", False)
    if isinstance(enabled_value, str):
        enabled = enabled_value.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(enabled_value)

    return {
        "enabled": enabled,
        "weekday": weekday,
        "time": parsed_time.strftime("%H:%M"),
        "timezone": timezone_name,
        "group": str(data.get("group", "")).strip(),
        "skip_dates": _normalize_date_list(
            data.get("skip_dates", []), "skip_dates"
        ),
        "extra_dates": _normalize_date_list(
            data.get("extra_dates", []), "extra_dates"
        ),
    }


def load_schedule(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_schedule()
    except OSError as exc:
        raise ValueError(f"cannot read schedule {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid schedule file {path}: {exc}") from exc
    return normalize_schedule(data)


def save_schedule(path: Path, schedule: dict[str, Any]) -> None:
    _save_json_object(path, normalize_schedule(schedule))


def schedule_slots(
    schedule: dict[str, Any], start: date, end: date
) -> list[datetime]:
    """Return scheduled datetimes between two dates, inclusive."""
    timezone = ZoneInfo(schedule["timezone"])
    hour, minute = (int(part) for part in schedule["time"].split(":"))
    skip = set(schedule["skip_dates"])
    extra = set(schedule["extra_dates"])

    slots: list[datetime] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        if iso not in skip and (
            day.weekday() == schedule["weekday"] or iso in extra
        ):
            slots.append(
                datetime.combine(
                    day, dt_time(hour, minute), tzinfo=timezone
                )
            )
        day += timedelta(days=1)
    return slots


def next_schedule_slots(
    schedule: dict[str, Any], now: datetime, count: int = 5
) -> list[datetime]:
    slots: list[datetime] = []
    day = now.date()
    last_day = day + timedelta(days=366)
    while day <= last_day and len(slots) < count:
        for slot in schedule_slots(schedule, day, day):
            if slot >= now:
                slots.append(slot)
                if len(slots) >= count:
                    break
        day += timedelta(days=1)
    return slots


def due_schedule_slot(
    schedule: dict[str, Any],
    now: datetime,
    grace: timedelta = SCHEDULE_GRACE,
) -> datetime | None:
    """Return a scheduled slot that is due now and still inside the grace period."""
    for days_back in range(0, 8):
        day = now.date() - timedelta(days=days_back)
        for slot in schedule_slots(schedule, day, day):
            if slot <= now <= slot + grace:
                return slot
    return None


def choose_poll(
    polls: list[dict[str, Any]],
    state: dict[str, Any],
    use_random: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Pick the next poll, advancing through the file in order by default."""
    last_index = state.get("last_index")
    if not isinstance(last_index, int) or not 0 <= last_index < len(polls):
        last_index = None

    if use_random:
        if len(polls) == 1:
            index = 0
        else:
            candidates = [
                index for index in range(len(polls)) if index != last_index
            ]
            index = random.choice(candidates)
    else:
        index = 0 if last_index is None else (last_index + 1) % len(polls)
    return index, polls[index]


_poll_rotation_lock = threading.Lock()


def send_next_poll(
    service: Any,
    polls_path: Path,
    poll_state_path: Path,
    group: str,
    use_random: bool = False,
) -> dict[str, Any]:
    """Select, send, and persist the next poll."""
    with _poll_rotation_lock:
        polls = _load_polls(polls_path)
        state = load_poll_state(poll_state_path)
        index, poll = choose_poll(polls, state, use_random)
        response = service.poll(group, poll["question"], poll["options"])
        state["last_index"] = index
        state["last_question"] = poll["question"]
        save_poll_state(poll_state_path, state)
        return {
            "index": index,
            "total": len(polls),
            "poll": poll,
            "response": response,
        }


class PollScheduler:
    def __init__(
        self,
        service: Any,
        polls_path: Path,
        poll_state_path: Path,
        schedule_path: Path,
        schedule_state_path: Path,
    ) -> None:
        self.service = service
        self.polls_path = polls_path
        self.poll_state_path = poll_state_path
        self.schedule_path = schedule_path
        self.schedule_state_path = schedule_state_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="wa-scheduler", daemon=True
        )
        self._thread.start()
        log.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(SCHEDULER_INTERVAL):
            try:
                self._tick()
            except Exception:
                log.exception("scheduler tick failed")

    def _tick(self) -> None:
        try:
            schedule = load_schedule(self.schedule_path)
        except ValueError as exc:
            log.error("Invalid schedule: %s", exc)
            return

        if not schedule["enabled"] or not schedule["group"]:
            return
        if not self.service.connected.is_set():
            return

        now = datetime.now(ZoneInfo(schedule["timezone"]))
        slot = due_schedule_slot(schedule, now)
        if slot is None:
            return

        state = load_schedule_state(self.schedule_state_path)
        if state.get("last_slot") == slot.isoformat():
            return

        result = send_next_poll(
            self.service,
            self.polls_path,
            self.poll_state_path,
            schedule["group"],
        )
        state.update(
            {
                "last_slot": slot.isoformat(),
                "last_sent_at": now.isoformat(),
                "last_question": result["poll"]["question"],
            }
        )
        save_schedule_state(self.schedule_state_path, state)
        log.info(
            "Scheduled poll sent for %s (%s): %s",
            slot.isoformat(),
            schedule["group"],
            result["poll"]["question"],
        )


def match_group(groups: list[dict[str, str]], query: str) -> dict[str, str]:
    """Resolve a group name or JID to a single joined group."""
    q = query.strip()
    if not q:
        raise ValueError("group name/JID is empty")

    if q.endswith("@g.us"):
        for group in groups:
            if group["jid"] == q:
                return group
        return {"jid": q, "name": q}

    folded = q.casefold()
    exact = [g for g in groups if g["name"].casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _raise_ambiguous(q, exact)

    partial = [g for g in groups if folded in g["name"].casefold()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        _raise_ambiguous(q, partial)

    names = ", ".join(sorted({g["name"] for g in groups}, key=str.casefold))
    raise LookupError(
        f"group {q!r} not found; joined groups: {names[:800] or 'none'}"
    )


def _raise_ambiguous(query: str, matches: list[dict[str, str]]) -> None:
    options = ", ".join(
        f"{g['name']} ({g['jid']})" for g in matches[:10]
    )
    raise LookupError(
        f"group {query!r} is ambiguous; use a JID: {options}"
    )


class WhatsAppService:
    def __init__(self, db_path: Path, qr_path: Path) -> None:
        self.db_path = db_path
        self.qr_path = qr_path
        self.client: Any = None
        self.connected = threading.Event()
        self.stop_requested = threading.Event()
        self.failure: str | None = None
        self._lock = threading.Lock()
        self._connect_thread: threading.Thread | None = None

    def start(self) -> None:
        from neonize.client import NewClient
        from neonize.events import (
            ConnectFailureEv,
            ConnectedEv,
            DisconnectedEv,
            LoggedOutEv,
            TemporaryBanEv,
        )

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass

        self.client = NewClient(str(self.db_path))
        self.client.event.qr(self._on_qr)

        @self.client.event(ConnectedEv)
        def _on_connected(client: Any, _event: Any) -> None:
            self.connected.set()
            self.failure = None
            account = self._account()
            log.info("Connected to WhatsApp%s", f" as {account}" if account else "")

        @self.client.event(DisconnectedEv)
        def _on_disconnected(_client: Any, event: Any) -> None:
            self.connected.clear()
            log.warning("Disconnected from WhatsApp (status=%s)", event.status)

        @self.client.event(LoggedOutEv)
        def _on_logged_out(_client: Any, _event: Any) -> None:
            self.connected.clear()
            self._fail("session was logged out; restart the daemon and scan a new QR code")

        @self.client.event(ConnectFailureEv)
        def _on_connect_failure(_client: Any, event: Any) -> None:
            self.connected.clear()
            self.failure = (
                f"connection failed (reason={event.Reason}, message={event.Message})"
            )
            log.warning("%s", self.failure)

        @self.client.event(TemporaryBanEv)
        def _on_temporary_ban(_client: Any, event: Any) -> None:
            self.connected.clear()
            self._fail(
                f"temporary ban (reason={event.Code}, expires={event.Expire})"
            )

        self._connect_thread = threading.Thread(
            target=self._connect_loop, name="wa-connect", daemon=True
        )
        self._connect_thread.start()

    def _connect_loop(self) -> None:
        try:
            self.client.connect()
        except Exception as exc:
            if not self.stop_requested.is_set():
                log.exception("WhatsApp connection stopped")
                self.failure = str(exc) or exc.__class__.__name__
        finally:
            self.connected.clear()
            self.stop_requested.set()

    def _fail(self, message: str) -> None:
        self.failure = message
        log.error("%s", message)
        self.stop_requested.set()

    def _on_qr(self, _client: Any, data: bytes) -> None:
        try:
            import segno

            qr = segno.make_qr(data)
            self.qr_path.parent.mkdir(parents=True, exist_ok=True)
            qr.save(self.qr_path, scale=8, border=2)
            if sys.stdout.isatty():
                qr.terminal(compact=True)
        except Exception:
            log.exception("could not render QR code")
        log.warning(
            "Scan %s with WhatsApp > Linked devices. The QR code rotates; "
            "the file is updated automatically.",
            self.qr_path,
        )

    def _account(self) -> str | None:
        from neonize.utils.jid import Jid2String

        if self.client is None or self.client.me is None:
            return None
        return Jid2String(self.client.me.JID)

    def _wait_connected(self) -> None:
        if self.client is None:
            raise RuntimeError("daemon is not initialized")
        if self.connected.is_set():
            return
        if self.stop_requested.is_set():
            raise RuntimeError(self.failure or "daemon is shutting down")
        if not self.connected.wait(CONNECT_WAIT):
            raise RuntimeError(
                self.failure
                or f"not connected after {CONNECT_WAIT:.0f}s; "
                "scan the QR code and check the daemon log"
            )

    def _joined_groups(self) -> list[dict[str, str]]:
        from neonize.utils.jid import Jid2String

        groups = [
            {
                "jid": Jid2String(info.JID),
                "name": info.GroupName.Name or "(unnamed)",
            }
            for info in self.client.get_joined_groups()
        ]
        groups.sort(key=lambda item: (item["name"].casefold(), item["jid"]))
        return groups

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected.is_set(),
            "account": self._account(),
            "failure": self.failure,
        }

    def groups(self) -> dict[str, Any]:
        self._wait_connected()
        with self._lock:
            return {"groups": self._joined_groups()}

    def _resolve_group(self, group: str) -> dict[str, str]:
        query = group.strip()
        if not query:
            raise ValueError("group name/JID is empty")
        if query.endswith("@g.us"):
            return {"jid": query, "name": query}
        return match_group(self._joined_groups(), query)

    def send(self, group: str, text: str, dry_run: bool = False) -> dict[str, Any]:
        text = normalize_text(text)
        self._wait_connected()
        with self._lock:
            resolved = self._resolve_group(group)
            if dry_run:
                return {
                    "dry_run": True,
                    "jid": resolved["jid"],
                    "name": resolved["name"],
                }
            response = self.client.send_message(resolved["jid"], text)
            log.info(
                "Sent message to %s (%s) id=%s",
                resolved["name"],
                resolved["jid"],
                response.ID,
            )
            return {
                "dry_run": False,
                "jid": resolved["jid"],
                "name": resolved["name"],
                "message_id": response.ID,
                "timestamp": response.Timestamp,
            }

    def poll(
        self,
        group: str,
        question: str,
        options: list[str],
        multiple: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        question, options = normalize_poll(question, options)
        self._wait_connected()
        with self._lock:
            resolved = self._resolve_group(group)
            if dry_run:
                return {
                    "dry_run": True,
                    "jid": resolved["jid"],
                    "name": resolved["name"],
                    "question": question,
                    "options": options,
                }

            from neonize.utils.enum import VoteType

            poll = self.client.build_poll_vote_creation(
                name=question,
                options=options,
                selectable_count=VoteType.MULTIPLE if multiple else VoteType.SINGLE,
            )
            response = self.client.send_message(resolved["jid"], poll)
            log.info(
                "Sent poll to %s (%s) id=%s",
                resolved["name"],
                resolved["jid"],
                response.ID,
            )
            return {
                "dry_run": False,
                "jid": resolved["jid"],
                "name": resolved["name"],
                "question": question,
                "options": options,
                "message_id": response.ID,
                "timestamp": response.Timestamp,
            }

    def dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return _error("request must be a JSON object")

        command = request.get("cmd")
        try:
            if command == "ping":
                return {"ok": True, **self.status()}
            if command == "status":
                return {"ok": True, **self.status()}
            if command == "groups":
                return {"ok": True, **self.groups()}
            if command == "send":
                group = request.get("group")
                text = request.get("text")
                if not isinstance(group, str) or not isinstance(text, str):
                    return _error("send requires string fields 'group' and 'text'")
                return {
                    "ok": True,
                    **self.send(group, text, bool(request.get("dry_run"))),
                }
            if command == "poll":
                group = request.get("group")
                question = request.get("question")
                options = request.get("options")
                if (
                    not isinstance(group, str)
                    or not isinstance(question, str)
                    or not isinstance(options, list)
                    or not all(isinstance(option, str) for option in options)
                ):
                    return _error(
                        "poll requires 'group', 'question', and a string 'options' list"
                    )
                return {
                    "ok": True,
                    **self.poll(
                        group,
                        question,
                        options,
                        bool(request.get("multiple")),
                        bool(request.get("dry_run")),
                    ),
                }
            return _error(f"unknown command: {command!r}")
        except (ValueError, LookupError, RuntimeError, TimeoutError) as exc:
            return _error(str(exc))
        except Exception as exc:
            log.exception("command %r failed", command)
            return _error(f"internal error: {exc}")

    def stop(self) -> None:
        self.stop_requested.set()
        if self.client is not None:
            try:
                self.client.stop()
            except Exception:
                log.debug("error while stopping client", exc_info=True)
        if self._connect_thread is not None:
            self._connect_thread.join(timeout=5)

    def connection_thread_alive(self) -> bool:
        return (
            self._connect_thread is not None and self._connect_thread.is_alive()
        )


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(75)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST:
                response = _error("request too large")
            else:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    response = _error(f"invalid JSON: {exc}")
                else:
                    service = getattr(self.server, "service")
                    response = service.dispatch(request)
        except (TimeoutError, socket.timeout):
            response = _error("request timed out")
        except Exception as exc:
            log.exception("socket request failed")
            response = _error(f"request failed: {exc}")

        try:
            self.wfile.write(
                json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
            )
        except OSError:
            pass


def _daemon_alive(socket_path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def serve(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.log_file)

    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(socket_path.parent, 0o700)
    except OSError:
        pass

    if socket_path.exists():
        if _daemon_alive(socket_path):
            log.error("another daemon is already listening on %s", socket_path)
            return 1
        socket_path.unlink()

    service = WhatsAppService(Path(args.db), Path(args.qr))
    service.start()

    polls_path = Path(args.polls)
    poll_state_path = Path(args.poll_state)
    schedule_path = Path(args.schedule)
    schedule_state_path = Path(args.schedule_state)

    scheduler = PollScheduler(
        service,
        polls_path,
        poll_state_path,
        schedule_path,
        schedule_state_path,
    )
    scheduler.start()

    web_server = None
    if not args.no_web:
        try:
            from web import create_app, start_web

            app = create_app(
                service,
                {
                    "polls_path": polls_path,
                    "poll_state_path": poll_state_path,
                    "schedule_path": schedule_path,
                    "schedule_state_path": schedule_state_path,
                    "parse_polls": parse_polls,
                    "load_schedule": load_schedule,
                    "save_schedule": save_schedule,
                    "normalize_schedule": normalize_schedule,
                    "schedule_slots": schedule_slots,
                    "next_schedule_slots": next_schedule_slots,
                    "send_next_poll": send_next_poll,
                    "load_poll_state": load_poll_state,
                    "load_schedule_state": load_schedule_state,
                    "default_group": os.environ.get("WA_GROUP", ""),
                    "weekdays": WEEKDAYS_DE,
                },
            )
            web_server = start_web(app, args.web_host, args.web_port)
            log.info(
                "Web UI on http://%s:%s", args.web_host, args.web_port
            )
        except Exception:
            log.exception("Could not start web UI")

    try:
        server = _ThreadingUnixServer(str(socket_path), _RequestHandler)
    except OSError:
        log.exception("Could not bind Unix socket %s", socket_path)
        scheduler.stop()
        if web_server is not None:
            web_server.shutdown()
            web_server.server_close()
        service.stop()
        return 1
    server.service = service  # type: ignore[attr-defined]
    try:
        os.chmod(socket_path, 0o600)
    except OSError:
        pass

    server_thread = threading.Thread(
        target=server.serve_forever, name="wa-socket", daemon=True
    )
    server_thread.start()
    log.info("Daemon ready (socket=%s, db=%s)", socket_path, args.db)

    stop = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        log.info("Received %s", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop.wait(0.5):
        if service.stop_requested.is_set():
            log.warning("Stopping: %s", service.failure or "connection ended")
            break

    scheduler.stop()
    if web_server is not None:
        web_server.shutdown()
        web_server.server_close()
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
    service.stop()
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    log.info("Stopped")
    exit_code = 1 if service.failure else 0

    # neonize 0.5.x can leave its non-daemon connection worker blocked in the
    # QR-channel loop after a disconnect while unpaired. Everything of ours is
    # already cleaned up, so force the process down instead of hanging forever.
    if service.connection_thread_alive():
        log.warning("connection worker did not stop; forcing exit")
        os._exit(exit_code)
    return exit_code


def _request(
    socket_path: Path, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
    except OSError as exc:
        client.close()
        raise RuntimeError(
            f"cannot reach daemon at {socket_path}: {exc}. "
            "Is `wa.py serve` running?"
        ) from exc

    try:
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = client.recv(65_536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_RESPONSE:
                raise RuntimeError("daemon response is too large")
    except OSError as exc:
        raise RuntimeError(f"daemon request failed: {exc}") from exc
    finally:
        client.close()

    if not buffer:
        raise RuntimeError("daemon closed the connection without a response")
    try:
        response = json.loads(buffer.split(b"\n", 1)[0])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid response from daemon: {exc}") from exc
    if not isinstance(response, dict):
        raise RuntimeError("invalid response from daemon")
    return response


def _print_error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def cmd_send(args: argparse.Namespace) -> int:
    text = args.text
    if text is None and args.text_file:
        try:
            text = Path(args.text_file).read_text(encoding="utf-8")
        except OSError as exc:
            _print_error(f"cannot read text file: {exc}")
            return 1
    if text is None:
        if sys.stdin.isatty():
            _print_error("provide --text, --text-file, or pipe the message on stdin")
            return 2
        text = sys.stdin.read()

    try:
        text = normalize_text(text)
    except ValueError as exc:
        _print_error(str(exc))
        return 2

    group = args.group or os.environ.get("WA_GROUP")
    if not group:
        _print_error("no group given; use --group or set WA_GROUP")
        return 2

    try:
        response = _request(
            Path(args.socket),
            {"cmd": "send", "group": group, "text": text, "dry_run": args.dry_run},
            args.timeout,
        )
    except RuntimeError as exc:
        _print_error(str(exc))
        return 1

    if not response.get("ok"):
        _print_error(str(response.get("error", "send failed")))
        return 1

    if response.get("dry_run"):
        print(f"would send to {response['name']} ({response['jid']})")
    else:
        print(
            f"sent to {response['name']} ({response['jid']}) "
            f"id={response.get('message_id', '?')}"
        )
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    options = args.options or DEFAULT_POLL_OPTIONS
    try:
        question, options = normalize_poll(args.question, options)
    except ValueError as exc:
        _print_error(str(exc))
        return 2

    group = args.group or os.environ.get("WA_GROUP")
    if not group:
        _print_error("no group given; use --group or set WA_GROUP")
        return 2

    try:
        response = _request(
            Path(args.socket),
            {
                "cmd": "poll",
                "group": group,
                "question": question,
                "options": options,
                "multiple": args.multiple,
                "dry_run": args.dry_run,
            },
            args.timeout,
        )
    except RuntimeError as exc:
        _print_error(str(exc))
        return 1

    if not response.get("ok"):
        _print_error(str(response.get("error", "poll failed")))
        return 1

    if response.get("dry_run"):
        print(
            f"would send poll to {response['name']} ({response['jid']}): "
            f"{response['question']} [{', '.join(response['options'])}]"
        )
    else:
        print(
            f"sent poll to {response['name']} ({response['jid']}) "
            f"id={response.get('message_id', '?')}"
        )
    return 0


def _load_polls(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read poll list {path}: {exc}") from exc
    return parse_polls(text)


def cmd_poll_next(args: argparse.Namespace) -> int:
    polls_path = Path(args.file)
    state_path = Path(args.state)
    try:
        polls = _load_polls(polls_path)
        state = load_poll_state(state_path)
        index, poll = choose_poll(polls, state, args.random)
    except ValueError as exc:
        _print_error(str(exc))
        return 2

    group = args.group or os.environ.get("WA_GROUP")
    if not group:
        _print_error("no group given; use --group or set WA_GROUP")
        return 2

    try:
        response = _request(
            Path(args.socket),
            {
                "cmd": "poll",
                "group": group,
                "question": poll["question"],
                "options": poll["options"],
                "multiple": args.multiple,
                "dry_run": args.dry_run,
            },
            args.timeout,
        )
    except RuntimeError as exc:
        _print_error(str(exc))
        return 1

    if not response.get("ok"):
        _print_error(str(response.get("error", "poll failed")))
        return 1

    label = f"{index + 1}/{len(polls)}"
    if response.get("dry_run"):
        print(
            f"[dry-run] next poll {label}: {poll['question']} "
            f"[{', '.join(poll['options'])}]"
        )
        return 0

    state["last_index"] = index
    state["last_question"] = poll["question"]
    try:
        save_poll_state(state_path, state)
    except OSError as exc:
        _print_error(f"poll sent but could not save rotation state: {exc}")
        return 1

    print(
        f"sent poll {label} to {response['name']} ({response['jid']}) "
        f"id={response.get('message_id', '?')}"
    )
    return 0


def cmd_polls(args: argparse.Namespace) -> int:
    try:
        polls = _load_polls(Path(args.file))
        state = load_poll_state(Path(args.state))
        index, _poll = choose_poll(polls, state)
    except ValueError as exc:
        _print_error(str(exc))
        return 2

    for poll_index, poll in enumerate(polls):
        marker = "->" if poll_index == index else "  "
        print(f"{marker} {poll_index + 1}. {poll['question']}")
        print(f"     answers: {', '.join(poll['options'])}")

    last_index = state.get("last_index")
    if isinstance(last_index, int) and 0 <= last_index < len(polls):
        print(f"\nlast sent: #{last_index + 1}")
    print(f"next: #{index + 1} of {len(polls)}")
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    try:
        response = _request(Path(args.socket), {"cmd": "groups"}, args.timeout)
    except RuntimeError as exc:
        _print_error(str(exc))
        return 1

    if not response.get("ok"):
        _print_error(str(response.get("error", "could not list groups")))
        return 1

    groups = response.get("groups", [])
    if args.json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
    else:
        for group in groups:
            print(f"{group['name']}\t{group['jid']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        response = _request(Path(args.socket), {"cmd": "status"}, args.timeout)
    except RuntimeError as exc:
        _print_error(str(exc))
        return 1

    if not response.get("ok"):
        _print_error(str(response.get("error", "status failed")))
        return 1

    if args.json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0

    print(f"connected: {response.get('connected')}")
    if response.get("account"):
        print(f"account:   {response['account']}")
    if response.get("failure"):
        print(f"failure:   {response['failure']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--socket",
        default=os.environ.get("WA_SOCKET", str(DEFAULT_SOCKET)),
        help=f"daemon Unix socket (default: {DEFAULT_SOCKET})",
    )

    parser = argparse.ArgumentParser(
        description="Send WhatsApp group messages through a local daemon."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser(
        "serve", parents=[common], help="run the WhatsApp daemon"
    )
    serve_parser.add_argument(
        "--db", default=os.environ.get("WA_DB", str(DEFAULT_DB)),
        help=f"session database path (default: {DEFAULT_DB})",
    )
    serve_parser.add_argument(
        "--qr", default=str(DEFAULT_QR),
        help=f"where to write the pairing QR code (default: {DEFAULT_QR})",
    )
    serve_parser.add_argument("--log-file", help="also write logs to this file")
    serve_parser.add_argument("-v", "--verbose", action="store_true")
    serve_parser.add_argument(
        "--polls",
        default=str(DEFAULT_POLLS_FILE),
        help=f"poll list file (default: {DEFAULT_POLLS_FILE})",
    )
    serve_parser.add_argument(
        "--poll-state",
        default=str(DEFAULT_POLL_STATE),
        help=f"poll rotation state (default: {DEFAULT_POLL_STATE})",
    )
    serve_parser.add_argument(
        "--schedule",
        default=str(DEFAULT_SCHEDULE_FILE),
        help=f"schedule file (default: {DEFAULT_SCHEDULE_FILE})",
    )
    serve_parser.add_argument(
        "--schedule-state",
        default=str(DEFAULT_SCHEDULE_STATE),
        help=f"schedule state (default: {DEFAULT_SCHEDULE_STATE})",
    )
    serve_parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="web UI bind address (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--web-port", type=int, default=8080, help="web UI port (default: 8080)"
    )
    serve_parser.add_argument(
        "--no-web", action="store_true", help="disable the web UI"
    )
    serve_parser.set_defaults(func=serve)

    send_parser = subparsers.add_parser(
        "send", parents=[common], help="send a message to a group"
    )
    send_parser.add_argument(
        "--group", help="group name or JID (or set WA_GROUP)"
    )
    send_parser.add_argument("--text", help="message text")
    send_parser.add_argument("--text-file", help="read the message from a file")
    send_parser.add_argument(
        "--dry-run", action="store_true", help="resolve the group without sending"
    )
    send_parser.add_argument(
        "--timeout", type=float, default=60.0, help="socket timeout in seconds"
    )
    send_parser.set_defaults(func=cmd_send)

    poll_parser = subparsers.add_parser(
        "poll", parents=[common], help="send a WhatsApp poll to a group"
    )
    poll_parser.add_argument(
        "--group", help="group name or JID (or set WA_GROUP)"
    )
    poll_parser.add_argument(
        "--question", default="AH-Training am Mittwoch – bist du dabei?"
    )
    poll_parser.add_argument(
        "--option",
        dest="options",
        action="append",
        help="poll option; repeat it (default: Yes, No, Maybe)",
    )
    poll_parser.add_argument(
        "--multiple",
        action="store_true",
        help="allow selecting more than one option",
    )
    poll_parser.add_argument(
        "--dry-run", action="store_true", help="resolve the group without sending"
    )
    poll_parser.add_argument(
        "--timeout", type=float, default=60.0, help="socket timeout in seconds"
    )
    poll_parser.set_defaults(func=cmd_poll)

    poll_next_parser = subparsers.add_parser(
        "poll-next",
        parents=[common],
        help="send the next poll from the editable list",
    )
    poll_next_parser.add_argument(
        "--group", help="group name or JID (or set WA_GROUP)"
    )
    poll_next_parser.add_argument(
        "--file",
        default=str(DEFAULT_POLLS_FILE),
        help=f"poll list file (default: {DEFAULT_POLLS_FILE})",
    )
    poll_next_parser.add_argument(
        "--state",
        default=str(DEFAULT_POLL_STATE),
        help=f"rotation state file (default: {DEFAULT_POLL_STATE})",
    )
    poll_next_parser.add_argument(
        "--random",
        action="store_true",
        help="pick randomly instead of going through the list in order",
    )
    poll_next_parser.add_argument(
        "--multiple",
        action="store_true",
        help="allow selecting more than one option",
    )
    poll_next_parser.add_argument(
        "--dry-run", action="store_true", help="show the next poll without sending"
    )
    poll_next_parser.add_argument(
        "--timeout", type=float, default=60.0, help="socket timeout in seconds"
    )
    poll_next_parser.set_defaults(func=cmd_poll_next)

    polls_parser = subparsers.add_parser(
        "polls", help="preview the poll list and the next selection"
    )
    polls_parser.add_argument(
        "--file",
        default=str(DEFAULT_POLLS_FILE),
        help=f"poll list file (default: {DEFAULT_POLLS_FILE})",
    )
    polls_parser.add_argument(
        "--state",
        default=str(DEFAULT_POLL_STATE),
        help=f"rotation state file (default: {DEFAULT_POLL_STATE})",
    )
    polls_parser.set_defaults(func=cmd_polls)

    groups_parser = subparsers.add_parser(
        "groups", parents=[common], help="list joined groups and their JIDs"
    )
    groups_parser.add_argument("--json", action="store_true")
    groups_parser.add_argument("--timeout", type=float, default=60.0)
    groups_parser.set_defaults(func=cmd_groups)

    status_parser = subparsers.add_parser(
        "status", parents=[common], help="show daemon connection status"
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--timeout", type=float, default=10.0)
    status_parser.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "serve":
        configure_logging()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
