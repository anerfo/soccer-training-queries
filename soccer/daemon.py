#!/usr/bin/env python3
"""Send WhatsApp group messages from a small local daemon.

The daemon owns the WhatsApp session (neonize/whatsmeow) and listens on a
Unix socket. The CLI can then be used from cron or by hand without keeping a
second WhatsApp connection open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .calendar import CalendarScheduler

BASE_DIR = Path(os.environ.get("APP_HOME") or Path(__file__).resolve().parent.parent)
DEFAULT_DATA_DIR = BASE_DIR / "data"
DEFAULT_SOCKET = DEFAULT_DATA_DIR / "wa.sock"
DEFAULT_DB = DEFAULT_DATA_DIR / "wa_session.db"
DEFAULT_QR = DEFAULT_DATA_DIR / "qr.png"
DEFAULT_POLLS_FILE = BASE_DIR / "polls.txt"
DEFAULT_POLL_STATE = DEFAULT_DATA_DIR / "polls_state.json"
DEFAULT_POLL_HISTORY = DEFAULT_DATA_DIR / "poll_history.json"
DEFAULT_CONTACTS = DEFAULT_DATA_DIR / "contacts.json"
DEFAULT_CALENDARS = DEFAULT_DATA_DIR / "calendars.json"
DEFAULT_CALENDAR_STATE = DEFAULT_DATA_DIR / "calendar_state.json"
POLL_HISTORY_LIMIT = 50
GROUPS_CACHE_SECONDS = 10


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
    logging.getLogger("neonize").setLevel(logging.DEBUG if verbose else logging.WARNING)


def normalize_text(text: str) -> str:
    if not text or not text.strip():
        raise ValueError("message text is empty")
    size = len(text.encode("utf-8"))
    if size > MAX_MESSAGE_BYTES:
        raise ValueError(f"message is too long ({size} bytes, max {MAX_MESSAGE_BYTES})")
    return text


def normalize_poll(question: str, options: list[str]) -> tuple[str, list[str]]:
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
        raise ValueError(f"poll options must be at most {MAX_POLL_OPTION} characters")
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
                f"line {line_number}: expected 'question | answer 1 | answer 2'"
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


def load_poll_history(path: Path) -> dict[str, Any]:
    history = _load_json_object(path)
    if not isinstance(history.get("polls"), list):
        history["polls"] = []
    return history


def save_poll_history(path: Path, history: dict[str, Any]) -> None:
    _save_json_object(path, history)


def load_contacts(path: Path) -> dict[str, str]:
    contacts = _load_json_object(path)
    return {
        str(key): value for key, value in contacts.items() if isinstance(value, str)
    }


def save_contacts(path: Path, contacts: dict[str, str]) -> None:
    _save_json_object(path, contacts)


def normalize_epoch(value: Any) -> int:
    """Return Unix seconds, accepting s/ms/µs/ns timestamps."""
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return 0
    if stamp <= 0:
        return 0
    while stamp > 1e11:
        stamp /= 1000.0
    return int(stamp)


def poll_option_hashes(options: list[str]) -> dict[str, str]:
    """Map hex(SHA256(option name)) to the option name.

    WhatsApp encodes a poll vote as the SHA-256 hashes of the selected option
    names, so this lets us translate a decrypted vote back to the answers.
    """
    return {
        hashlib.sha256(option.encode("utf-8")).hexdigest(): option for option in options
    }


class PollHistoryStore:
    """Records sent polls and the votes received for them."""

    def __init__(self, path: Path, limit: int = POLL_HISTORY_LIMIT) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()

    def record_sent_poll(
        self,
        *,
        message_id: str,
        chat: str,
        group_name: str,
        question: str,
        options: list[str],
        timestamp: Any,
        simulated: bool = False,
    ) -> None:
        if not message_id:
            return
        with self._lock:
            history = load_poll_history(self.path)
            polls = [
                poll
                for poll in history["polls"]
                if poll.get("message_id") != message_id
            ]
            entry = {
                "message_id": message_id,
                "chat": chat,
                "group_name": group_name,
                "question": question,
                "options": options,
                "sent_at": normalize_epoch(timestamp),
                "votes": {},
            }
            if simulated:
                entry["simulated"] = True
            polls.insert(0, entry)
            history["polls"] = polls[: self.limit]
            save_poll_history(self.path, history)

    def delete_poll(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._lock:
            history = load_poll_history(self.path)
            before = len(history["polls"])
            history["polls"] = [
                poll
                for poll in history["polls"]
                if poll.get("message_id") != message_id
            ]
            if len(history["polls"]) == before:
                return False
            save_poll_history(self.path, history)
            return True

    def clear_all(self) -> int:
        with self._lock:
            history = load_poll_history(self.path)
            removed = len(history["polls"])
            if removed:
                history["polls"] = []
                save_poll_history(self.path, history)
            return removed

    def clear_simulated(self) -> int:
        with self._lock:
            history = load_poll_history(self.path)
            before = len(history["polls"])
            history["polls"] = [
                poll for poll in history["polls"] if not poll.get("simulated")
            ]
            removed = before - len(history["polls"])
            if removed:
                save_poll_history(self.path, history)
            return removed

    def record_vote(
        self,
        *,
        poll_id: str,
        chat: str,
        sender: str,
        sender_alt: str,
        pushname: str,
        selected_hashes: list[str],
        timestamp: Any,
    ) -> bool:
        if not poll_id:
            return False
        with self._lock:
            history = load_poll_history(self.path)
            for poll in history["polls"]:
                if poll.get("message_id") != poll_id:
                    continue
                option_map = poll_option_hashes(poll.get("options", []))
                selected = [
                    option_map.get(option_hash, option_hash)
                    for option_hash in selected_hashes
                ]
                key = sender or sender_alt or pushname or ""
                poll.setdefault("votes", {})[key] = {
                    "sender": sender,
                    "sender_alt": sender_alt,
                    "pushname": pushname,
                    "selected": selected,
                    "timestamp": normalize_epoch(timestamp),
                }
                save_poll_history(self.path, history)
                return True
            return False


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
            candidates = [index for index in range(len(polls)) if index != last_index]
            index = random.choice(candidates)  # nosec B311
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
    raise LookupError(f"group {q!r} not found; joined groups: {names[:800] or 'none'}")


def _raise_ambiguous(query: str, matches: list[dict[str, str]]) -> None:
    options = ", ".join(f"{g['name']} ({g['jid']})" for g in matches[:10])
    raise LookupError(f"group {query!r} is ambiguous; use a JID: {options}")


def _install_poll_vote_workaround() -> None:
    """Fix neonize 0.5.2's broken ``DecryptPollVote`` FFI return type.

    The library attaches its ``consume_bytes_struct`` errcheck to most
    functions returning ``*C.struct_BytesReturn`` but forgot
    ``DecryptPollVote``. Without the errcheck the call yields a ``LP_Bytes``
    pointer and protobuf parsing fails with
    ``TypeError: expected bytes, LP_Bytes found``.
    """
    try:
        import ctypes

        from neonize import _binder
    except Exception:
        log.debug("could not patch neonize poll vote binding", exc_info=True)
        return

    gocode = getattr(_binder, "gocode", None)
    if gocode is None or not hasattr(gocode, "DecryptPollVote"):
        return

    def _consume(result: Any, _func: Any, _args: Any) -> bytes:
        if not result:
            return b""
        try:
            contents = result.contents
            if not contents.ptr or contents.size == 0:
                return b""
            return ctypes.string_at(contents.ptr, contents.size)
        finally:
            gocode.FreeBytesStruct(result)

    gocode.DecryptPollVote.errcheck = _consume


class WhatsAppService:
    def __init__(
        self,
        db_path: Path,
        qr_path: Path,
        history_path: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.qr_path = qr_path
        self.history = PollHistoryStore(
            history_path or (db_path.parent / DEFAULT_POLL_HISTORY.name)
        )
        self.client: Any = None
        self.connected = threading.Event()
        self.stop_requested = threading.Event()
        self.failure: str | None = None
        self._lock = threading.Lock()
        self._connect_thread: threading.Thread | None = None
        self._groups_cache: list[dict[str, str]] | None = None
        self._groups_cache_at = 0.0
        self._groups_cache_lock = threading.Lock()

    def start(self) -> None:
        from neonize.client import NewClient
        from neonize.events import (
            ConnectedEv,
            ConnectFailureEv,
            DisconnectedEv,
            LoggedOutEv,
            MessageEv,
            TemporaryBanEv,
        )

        _install_poll_vote_workaround()

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
            self._fail(
                "session was logged out; restart the daemon and scan a new QR code"
            )

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
            self._fail(f"temporary ban (reason={event.Code}, expires={event.Expire})")

        @self.client.event(MessageEv)
        def _on_message(_client: Any, event: Any) -> None:
            try:
                self._handle_message(event)
            except Exception:
                log.exception("could not process incoming message")

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
            tmp_path = self.qr_path.with_suffix(".tmp.png")
            qr.save(str(tmp_path), scale=8, border=2)
            os.replace(tmp_path, self.qr_path)
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

    def _handle_message(self, event: Any) -> None:
        """Record incoming poll votes."""
        from neonize.utils.jid import Jid2String
        from neonize.utils.message import get_poll_update_message

        update = get_poll_update_message(event)
        if update is None:
            return

        poll_id = update.pollCreationMessageKey.ID
        source = event.Info.MessageSource
        chat = Jid2String(source.Chat) if source.HasField("Chat") else ""
        sender = Jid2String(source.Sender) if source.HasField("Sender") else ""
        sender_alt = (
            Jid2String(source.SenderAlt) if source.HasField("SenderAlt") else ""
        )

        try:
            vote = self.client.decrypt_poll_vote(event)
            selected_hashes = [bytes(item).hex() for item in vote.selectedOptions]
        except Exception:
            log.warning(
                "could not decrypt poll vote for %s; the original message "
                "secret may not be stored",
                poll_id,
                exc_info=True,
            )
            return

        recorded = self.history.record_vote(
            poll_id=poll_id,
            chat=chat,
            sender=sender,
            sender_alt=sender_alt,
            pushname=event.Info.Pushname,
            selected_hashes=selected_hashes,
            timestamp=event.Info.Timestamp,
        )
        log.info(
            "Recorded poll vote for %s from %s (%d option(s))%s",
            poll_id,
            sender or sender_alt or "(unknown)",
            len(selected_hashes),
            "" if recorded else " [poll not in history]",
        )

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
            "qr_available": self.qr_path.is_file(),
        }

    def groups(self) -> dict[str, Any]:
        self._wait_connected()
        with self._lock:
            return {"groups": self._joined_groups()}

    def _cached_groups(self) -> list[dict[str, str]]:
        now = time.monotonic()
        with self._groups_cache_lock:
            if (
                self._groups_cache is not None
                and now - self._groups_cache_at < GROUPS_CACHE_SECONDS
            ):
                return self._groups_cache
        groups = self._joined_groups()
        with self._groups_cache_lock:
            self._groups_cache = groups
            self._groups_cache_at = now
        return groups

    def group_check(self, query: str) -> dict[str, Any]:
        """Tell whether a group name/JID currently exists in the account."""
        query = (query or "").strip()
        if not query:
            return {"available": True, "exists": False}
        if not self.connected.is_set():
            return {"available": False, "exists": False}
        try:
            with self._lock:
                groups = self._cached_groups()
        except Exception as exc:
            log.warning("group check failed: %s", exc)
            return {
                "available": False,
                "exists": False,
                "error": str(exc),
            }

        if query.endswith("@g.us"):
            for group in groups:
                if group["jid"] == query:
                    return {"available": True, "exists": True, **group}
            return {"available": True, "exists": False}

        try:
            resolved = match_group(groups, query)
        except (LookupError, ValueError) as exc:
            return {
                "available": True,
                "exists": False,
                "error": str(exc),
            }
        return {"available": True, "exists": True, **resolved}

    @staticmethod
    def _participant_number(participant: Any) -> str:
        from neonize.utils.jid import Jid2String

        for field in ("PhoneNumber", "JID"):
            if not participant.HasField(field):
                continue
            jid = getattr(participant, field)
            if not jid.User:
                continue
            string = Jid2String(jid)
            server = string.split("@", 1)[1] if "@" in string else ""
            if server == "s.whatsapp.net":
                return string.split("@", 1)[0].split(":", 1)[0]
        return ""

    def _contact_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (address-book names, WhatsApp pushnames) keyed by number."""
        from neonize.utils.jid import Jid2String

        names: dict[str, str] = {}
        pushnames: dict[str, str] = {}
        try:
            contacts = self.client.contact.get_all_contacts()
        except Exception:
            log.warning("could not read the WhatsApp contact store", exc_info=True)
            return names, pushnames
        for contact in contacts:
            try:
                jid = Jid2String(contact.JID)
            except Exception:
                log.debug("could not decode contact JID", exc_info=True)
                continue
            number = jid.split("@", 1)[0].split(":", 1)[0]
            info = contact.Info
            pushname = (info.PushName or "").strip()
            name = (
                info.FullName or info.FirstName or info.BusinessName or pushname or ""
            ).strip()
            if number and name:
                names[number] = name
            if number and pushname:
                pushnames[number] = pushname
        return names, pushnames

    def group_directory(self, group: str) -> dict[str, dict[str, str]]:
        """Return {phone number: {"name": ..., "whatsapp": ...}} for a group.

        ``name`` prefers the phone's address book and falls back to the
        WhatsApp display name. ``whatsapp`` is the name WhatsApp shows for the
        participant (display/push name).
        """
        from neonize.utils.jid import build_jid

        self._wait_connected()
        with self._lock:
            resolved = self._resolve_group(group)
            user, _, server = resolved["jid"].partition("@")
            group_jid = build_jid(user, server or "g.us")
            info = self.client.get_group_info(group_jid)
            names, pushnames = self._contact_maps()
            result: dict[str, dict[str, str]] = {}
            for participant in info.Participants:
                number = self._participant_number(participant)
                if not number:
                    continue
                display = (participant.DisplayName or "").strip()
                whatsapp = display or pushnames.get(number, "")
                result[number] = {
                    "name": names.get(number) or whatsapp,
                    "whatsapp": whatsapp,
                }
            return result

    def group_contacts(self, group: str) -> dict[str, str]:
        """Return {phone number: name} for the members of a group."""
        return {
            number: entry["name"]
            for number, entry in self.group_directory(group).items()
        }

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
            self.history.record_sent_poll(
                message_id=response.ID,
                chat=resolved["jid"],
                group_name=resolved["name"],
                question=question,
                options=options,
                timestamp=response.Timestamp,
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
        return self._connect_thread is not None and self._connect_thread.is_alive()


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
                    service = self.server.service  # type: ignore[attr-defined]
                    response = service.dispatch(request)
        except TimeoutError:
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

    polls_path = Path(args.polls)
    poll_state_path = Path(args.poll_state)
    poll_history_path = Path(args.poll_history)
    contacts_path = Path(args.contacts)
    calendars_path = Path(args.calendars)
    calendar_state_path = Path(args.calendar_state)

    service = WhatsAppService(Path(args.db), Path(args.qr), poll_history_path)
    service.start()

    calendar_scheduler = CalendarScheduler(
        service,
        calendars_path,
        calendar_state_path,
        polls_path,
        parse_polls,
    )
    calendar_scheduler.start()

    web_server = None
    if not args.no_web:
        try:
            from .web import create_app, start_web

            app = create_app(
                service,
                {
                    "polls_path": polls_path,
                    "poll_state_path": poll_state_path,
                    "qr_path": Path(args.qr),
                    "parse_polls": parse_polls,
                    "send_next_poll": send_next_poll,
                    "choose_poll": choose_poll,
                    "load_poll_state": load_poll_state,
                    "poll_history_path": poll_history_path,
                    "load_poll_history": load_poll_history,
                    "contacts_path": contacts_path,
                    "load_contacts": load_contacts,
                    "save_contacts": save_contacts,
                    "group_contacts": service.group_contacts,
                    "group_directory": service.group_directory,
                    "group_check": service.group_check,
                    "poll_history_store": service.history,
                    "calendar_config_path": calendars_path,
                    "calendar_state_path": calendar_state_path,
                    "calendar_scheduler": calendar_scheduler,
                    "default_group": os.environ.get("WA_GROUP", ""),
                },
            )
            web_server = start_web(app, args.web_host, args.web_port)
            log.info("Web UI on http://%s:%s", args.web_host, args.web_port)
        except Exception:
            log.exception("Could not start web UI")

    try:
        server = _ThreadingUnixServer(str(socket_path), _RequestHandler)
    except OSError:
        log.exception("Could not bind Unix socket %s", socket_path)
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

    calendar_scheduler.stop()
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
            f"cannot reach daemon at {socket_path}: {exc}. Is `wa.py serve` running?"
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
        "--db",
        default=os.environ.get("WA_DB", str(DEFAULT_DB)),
        help=f"session database path (default: {DEFAULT_DB})",
    )
    serve_parser.add_argument(
        "--qr",
        default=str(DEFAULT_QR),
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
        "--poll-history",
        default=str(DEFAULT_POLL_HISTORY),
        help=f"sent polls and votes (default: {DEFAULT_POLL_HISTORY})",
    )
    serve_parser.add_argument(
        "--contacts",
        default=str(DEFAULT_CONTACTS),
        help=f"number to name mapping (default: {DEFAULT_CONTACTS})",
    )
    serve_parser.add_argument(
        "--calendars",
        default=str(DEFAULT_CALENDARS),
        help=f"calendar/rule configuration (default: {DEFAULT_CALENDARS})",
    )
    serve_parser.add_argument(
        "--calendar-state",
        default=str(DEFAULT_CALENDAR_STATE),
        help=f"calendar send state (default: {DEFAULT_CALENDAR_STATE})",
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
    send_parser.add_argument("--group", help="group name or JID (or set WA_GROUP)")
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
    poll_parser.add_argument("--group", help="group name or JID (or set WA_GROUP)")
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
    poll_next_parser.add_argument("--group", help="group name or JID (or set WA_GROUP)")
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
