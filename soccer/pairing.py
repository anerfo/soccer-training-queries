"""Manage the passkey pairing worker and adopt its WhatsApp session.

The worker is a small Go binary (``tools/pair``) that uses the same whatsmeow
version as neonize. It is started on demand, binds to localhost only, and is
guarded by a random token. When pairing succeeds its fresh session database is
moved over the daemon's database.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess  # nosec B404
import threading
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("wa.pairing")

DEFAULT_PAIR_BIN = "wa-pair"
START_RETRY_SECONDS = 45.0
CHALLENGE_TIMEOUT = 55.0
RESPONSE_TIMEOUT = 55.0
STATUS_TIMEOUT = 5.0
STOP_TIMEOUT = 8.0


class PairingError(RuntimeError):
    """Raised when the pairing worker cannot complete a request."""


class PairingManager:
    """Own the pairing worker process and the session adoption step."""

    def __init__(
        self,
        db_path: Path,
        *,
        binary: str | None = None,
        fresh_db_path: Path | None = None,
        auto_restart: bool | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.fresh_db_path = fresh_db_path or self.db_path.with_name(
            f"{self.db_path.stem}_new{self.db_path.suffix}"
        )
        self.binary = binary or os.environ.get("WA_PAIR_BIN") or DEFAULT_PAIR_BIN
        self.auto_restart = (
            _env_flag("WA_AUTO_RESTART") if auto_restart is None else auto_restart
        )
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._log_file: Any = None
        self._port = 0
        self._token = ""  # nosec B105
        self._finished = False

    def start(self, *, restart: bool = False) -> dict[str, Any]:
        """Start the worker, or return the running instance."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                if not restart:
                    return {"ok": True, "running": True, "started": False}
                self._stop_worker()
            self._finished = False
            self._remove_fresh_db()
            binary = self._resolve_binary()
            self._port = _pick_port()
            self._token = secrets.token_hex(16)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = self.db_path.parent / "pair-worker.log"
            self._log_file = log_path.open("a", encoding="utf-8")
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
            env = {**os.environ, "WA_PAIR_TOKEN": self._token}
            try:
                # Binary is resolved via shutil.which; no shell is used.
                self._proc = subprocess.Popen(  # nosec B603
                    [
                        binary,
                        "--db",
                        str(self.fresh_db_path),
                        "--addr",
                        f"127.0.0.1:{self._port}",
                    ],
                    stdout=self._log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                    text=True,
                )
            except OSError as exc:
                self._close_log()
                raise PairingError(f"could not start pairing worker: {exc}") from exc
            log.info(
                "pairing worker started (pid=%s, port=%s)",
                self._proc.pid,
                self._port,
            )
            return {"ok": True, "running": True, "started": True}

    def challenge(self) -> dict[str, Any]:
        data = self._request(
            "/challenge", timeout=CHALLENGE_TIMEOUT, retry_for=START_RETRY_SECONDS
        )
        if "publicKey" not in data:
            raise PairingError("pairing worker did not return a challenge")
        return data

    def respond(self, assertion: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "/response",
            payload=assertion,
            timeout=RESPONSE_TIMEOUT,
            retry_for=5.0,
        )

    def status(self) -> dict[str, Any]:
        proc = self._proc
        if proc is None:
            return _idle_status()
        if proc.poll() is not None:
            return {
                **_idle_status(),
                "error": (
                    f"pairing worker exited with {proc.returncode}; "
                    "see data/pair-worker.log"
                ),
            }
        try:
            data = self._request("/status", timeout=STATUS_TIMEOUT)
        except PairingError as exc:
            return {**_idle_status(), "running": True, "error": str(exc)}
        return {"running": True, **data}

    def finish(self) -> dict[str, Any]:
        """Stop the worker, adopt its session and report whether to restart."""
        with self._lock:
            if self._finished:
                return {"ok": True, "restart": False, "already": True}
            status = self.status()
            if not (status.get("paired") or status.get("connected")):
                raise PairingError("pairing is not complete yet")
            self._stop_worker()
            self._adopt_session()
            self._finished = True
            log.info("passkey session adopted into %s", self.db_path)
            return {"ok": True, "restart": self.auto_restart}

    def abort(self) -> dict[str, Any]:
        with self._lock:
            self._stop_worker()
            self._remove_fresh_db()
            self._finished = False
            return {"ok": True}

    def stop(self) -> None:
        with self._lock:
            self._stop_worker()

    def _resolve_binary(self) -> str:
        path = shutil.which(self.binary)
        if not path:
            raise PairingError(
                f"pairing worker {self.binary!r} not found; build tools/pair "
                "or set WA_PAIR_BIN"
            )
        return path

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
        retry_for: float = 0.0,
    ) -> dict[str, Any]:
        proc = self._proc
        port = self._port
        token = self._token
        if proc is None or proc.poll() is not None or not port or not token:
            raise PairingError("pairing worker is not running")

        url = f"http://127.0.0.1:{port}{path}"
        headers = {"X-Pair-Token": token}
        deadline = time.monotonic() + retry_for
        while True:
            try:
                if payload is None:
                    response = requests.get(url, headers=headers, timeout=timeout)
                else:
                    response = requests.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    )
                break
            except requests.ConnectionError as exc:
                if time.monotonic() >= deadline:
                    raise PairingError(
                        "pairing worker is not reachable; see data/pair-worker.log"
                    ) from exc
                time.sleep(0.5)
            except requests.RequestException as exc:
                raise PairingError(f"pairing worker request failed: {exc}") from exc

        if response.status_code >= 400:
            message = response.text.strip() or f"HTTP {response.status_code}"
            raise PairingError(message)
        try:
            data = response.json()
        except ValueError as exc:
            raise PairingError("pairing worker returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise PairingError("pairing worker returned an unexpected response")
        return data

    def _adopt_session(self) -> None:
        fresh = self.fresh_db_path
        if not fresh.is_file() or fresh.stat().st_size == 0:
            raise PairingError("pairing worker did not write a session database")

        db = self.db_path
        backup = db.with_name(f"{db.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        if db.exists():
            db.replace(backup)
        for suffix in ("-wal", "-shm"):
            old = Path(str(db) + suffix)
            if old.exists():
                old.replace(Path(str(backup) + suffix))

        fresh.replace(db)
        for suffix in ("-wal", "-shm"):
            source = Path(str(fresh) + suffix)
            if source.exists():
                source.replace(Path(str(db) + suffix))

        for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    def _remove_fresh_db(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.fresh_db_path) + suffix)
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _stop_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    log.warning("pairing worker did not stop")
        self._close_log()
        self._port = 0
        self._token = ""  # nosec B105

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _idle_status() -> dict[str, Any]:
    return {
        "running": False,
        "paired": False,
        "connected": False,
        "code": "",
        "skipHandoff": False,
        "error": "",
    }


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
