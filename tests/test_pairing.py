import tempfile
import unittest
from pathlib import Path
from unittest import mock

from soccer.pairing import PairingError, PairingManager


class PairingManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = self.base / "wa_session.db"
        self.fresh = self.base / "wa_session_new.db"
        self.manager = PairingManager(
            self.db,
            fresh_db_path=self.fresh,
            binary="/usr/bin/true",
            auto_restart=False,
        )

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def _running_manager(self):
        self.manager._proc = mock.Mock()
        self.manager._proc.poll.return_value = None
        self.manager._port = 5555
        self.manager._token = "secret"

    def test_idle_status(self):
        status = self.manager.status()
        self.assertFalse(status["running"])
        self.assertFalse(status["paired"])
        self.assertFalse(status["connected"])

    def test_start_requires_worker_binary(self):
        manager = PairingManager(
            self.db,
            fresh_db_path=self.fresh,
            binary=str(self.base / "missing"),
            auto_restart=False,
        )
        with self.assertRaises(PairingError):
            manager.start()

    def test_start_passes_token_and_fresh_db(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.pid = 4242
        with (
            mock.patch("soccer.pairing.shutil.which", return_value="/usr/bin/wa-pair"),
            mock.patch("soccer.pairing.subprocess.Popen", return_value=proc) as popen,
        ):
            result = self.manager.start()

        self.assertTrue(result["started"])
        args = popen.call_args.args[0]
        self.assertEqual(args[0], "/usr/bin/wa-pair")
        self.assertIn(str(self.fresh), args)
        self.assertEqual(
            popen.call_args.kwargs["env"]["WA_PAIR_TOKEN"],
            self.manager._token,
        )
        self.manager.abort()

    def test_challenge_proxies_public_key(self):
        self._running_manager()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"publicKey": {"challenge": "abc"}}
        with mock.patch(
            "soccer.pairing.requests.get", return_value=response
        ) as request:
            data = self.manager.challenge()

        self.assertEqual(data["publicKey"]["challenge"], "abc")
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:5555/challenge")
        self.assertEqual(
            request.call_args.kwargs["headers"], {"X-Pair-Token": "secret"}
        )

    def test_response_posts_assertion(self):
        self._running_manager()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with mock.patch(
            "soccer.pairing.requests.post", return_value=response
        ) as request:
            result = self.manager.respond({"id": "credential"})

        self.assertTrue(result["ok"])
        self.assertEqual(request.call_args.kwargs["json"], {"id": "credential"})

    def test_worker_error_is_raised(self):
        self._running_manager()
        response = mock.Mock(status_code=502, text="no passkey")
        with mock.patch("soccer.pairing.requests.get", return_value=response):
            with self.assertRaises(PairingError) as raised:
                self.manager.challenge()
        self.assertIn("no passkey", str(raised.exception))

    def test_adopt_session_moves_database_and_wal(self):
        self.db.write_bytes(b"old")
        Path(str(self.db) + "-wal").write_bytes(b"old-wal")
        self.fresh.write_bytes(b"new")
        Path(str(self.fresh) + "-wal").write_bytes(b"new-wal")

        self.manager._adopt_session()

        self.assertEqual(self.db.read_bytes(), b"new")
        self.assertEqual(Path(str(self.db) + "-wal").read_bytes(), b"new-wal")
        backups = [
            path
            for path in self.base.glob("wa_session.db.bak-*")
            if not path.name.endswith("-wal")
        ]
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_bytes(), b"old")
        self.assertEqual(Path(str(backups[0]) + "-wal").read_bytes(), b"old-wal")
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_adopt_session_requires_data(self):
        with self.assertRaises(PairingError):
            self.manager._adopt_session()

    def test_remove_fresh_db_removes_sidecars(self):
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.fresh) + suffix).write_bytes(b"x")

        self.manager._remove_fresh_db()

        for suffix in ("", "-wal", "-shm"):
            self.assertFalse(Path(str(self.fresh) + suffix).exists())

    def test_finish_adopts_paired_session(self):
        self._running_manager()
        self.fresh.write_bytes(b"new")
        response = mock.Mock(status_code=200)
        response.json.return_value = {"paired": True, "connected": True}
        with mock.patch("soccer.pairing.requests.get", return_value=response):
            result = self.manager.finish()

        self.assertTrue(result["ok"])
        self.assertFalse(result["restart"])
        self.assertEqual(self.db.read_bytes(), b"new")

    def test_finish_requires_completed_pairing(self):
        self._running_manager()
        response = mock.Mock(status_code=200)
        response.json.return_value = {"paired": False, "connected": False}
        with mock.patch("soccer.pairing.requests.get", return_value=response):
            with self.assertRaises(PairingError):
                self.manager.finish()
