import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import wa
from web import create_app


class FakeService:
    def __init__(self, connected=True):
        self.connected = threading.Event()
        if connected:
            self.connected.set()
        self.sent = []

    def status(self):
        return {
            "connected": self.connected.is_set(),
            "account": None,
            "failure": None,
        }

    def poll(self, group, question, options, multiple=False, dry_run=False):
        self.sent.append(
            {
                "group": group,
                "question": question,
                "options": options,
                "multiple": multiple,
                "dry_run": dry_run,
            }
        )
        return {
            "dry_run": dry_run,
            "jid": "1@g.us",
            "name": group,
            "question": question,
            "options": options,
            "message_id": "msg-id",
            "timestamp": 1,
        }


def make_schedule(**overrides):
    data = {
        "enabled": True,
        "weekday": 2,
        "time": "18:00",
        "timezone": "Europe/Berlin",
        "group": "AH Training",
        "skip_dates": [],
        "extra_dates": [],
    }
    data.update(overrides)
    return wa.normalize_schedule(data)


class ScheduleLogicTests(unittest.TestCase):
    def test_normalize_schedule_trims_and_sorts_dates(self):
        schedule = make_schedule(
            enabled="yes",
            group="  AH Training  ",
            skip_dates="2026-12-23, 2026-12-30",
            extra_dates=["2026-11-04", "2026-11-04"],
        )
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["group"], "AH Training")
        self.assertEqual(schedule["skip_dates"], ["2026-12-23", "2026-12-30"])
        self.assertEqual(schedule["extra_dates"], ["2026-11-04"])

    def test_normalize_handles_string_booleans(self):
        self.assertFalse(make_schedule(enabled="false")["enabled"])
        self.assertTrue(make_schedule(enabled="true")["enabled"])

    def test_normalize_rejects_bad_time(self):
        with self.assertRaises(ValueError):
            make_schedule(time="25:00")

    def test_normalize_rejects_bad_timezone(self):
        with self.assertRaises(ValueError):
            make_schedule(timezone="Moon/Base")

    def test_slots_include_weekly_and_extra_but_skip_dates(self):
        schedule = make_schedule(
            weekday=2,
            skip_dates=["2026-10-28"],
            extra_dates=["2026-10-29"],
        )
        slots = wa.schedule_slots(
            schedule, date(2026, 10, 26), date(2026, 11, 4)
        )
        days = [slot.date().isoformat() for slot in slots]
        self.assertNotIn("2026-10-28", days)
        self.assertIn("2026-10-29", days)
        self.assertIn("2026-11-04", days)

    def test_due_slot_inside_grace(self):
        schedule = make_schedule()
        tz = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 10, 28, 18, 10, tzinfo=tz)
        slot = wa.due_schedule_slot(schedule, now)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.strftime("%Y-%m-%d %H:%M"), "2026-10-28 18:00")

    def test_due_slot_outside_grace(self):
        schedule = make_schedule()
        now = datetime(
            2026, 10, 28, 19, 0, tzinfo=ZoneInfo("Europe/Berlin")
        )
        self.assertIsNone(wa.due_schedule_slot(schedule, now))

    def test_next_slots_are_in_the_future(self):
        schedule = make_schedule()
        tz = ZoneInfo("Europe/Berlin")
        now = datetime(2026, 10, 26, 12, 0, tzinfo=tz)
        slots = wa.next_schedule_slots(schedule, now, 3)
        self.assertEqual(len(slots), 3)
        self.assertTrue(all(slot >= now for slot in slots))
        self.assertEqual(slots[0].strftime("%Y-%m-%d"), "2026-10-28")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.polls_path = base / "polls.txt"
        self.polls_path.write_text(
            "Erste? | Ja | Nein\nZweite? | Ja | Nein\n", encoding="utf-8"
        )
        self.poll_state = base / "polls_state.json"
        self.schedule_path = base / "schedule.json"
        self.schedule_state = base / "schedule_state.json"
        self.service = FakeService()

    def tearDown(self):
        self.temp.cleanup()

    def _save_schedule(self, **overrides):
        now = datetime.now(ZoneInfo("UTC"))
        data = {
            "enabled": True,
            "weekday": now.weekday(),
            "time": now.strftime("%H:%M"),
            "timezone": "UTC",
            "group": "AH Training",
            "skip_dates": [],
            "extra_dates": [],
        }
        data.update(overrides)
        wa.save_schedule(self.schedule_path, data)

    def _scheduler(self):
        return wa.PollScheduler(
            self.service,
            self.polls_path,
            self.poll_state,
            self.schedule_path,
            self.schedule_state,
        )

    def test_due_poll_is_sent_once(self):
        self._save_schedule()
        scheduler = self._scheduler()
        scheduler._tick()
        self.assertEqual(len(self.service.sent), 1)
        self.assertEqual(self.service.sent[0]["question"], "Erste?")

        scheduler._tick()
        self.assertEqual(len(self.service.sent), 1)

        poll_state = wa.load_poll_state(self.poll_state)
        self.assertEqual(poll_state["last_index"], 0)
        schedule_state = wa.load_schedule_state(self.schedule_state)
        self.assertIn("last_slot", schedule_state)

    def test_disabled_schedule_does_not_send(self):
        self._save_schedule(enabled=False)
        self._scheduler()._tick()
        self.assertEqual(self.service.sent, [])

    def test_skipped_day_does_not_send(self):
        now = datetime.now(ZoneInfo("UTC"))
        self._save_schedule(skip_dates=[now.date().isoformat()])
        self._scheduler()._tick()
        self.assertEqual(self.service.sent, [])


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.polls_path = base / "polls.txt"
        self.polls_path.write_text(
            "Erste? | Ja | Nein\nZweite? | Ja | Nein\n", encoding="utf-8"
        )
        self.poll_state = base / "polls_state.json"
        self.schedule_path = base / "schedule.json"
        self.schedule_state = base / "schedule_state.json"
        self.qr_path = base / "qr.png"
        self.poll_history_path = base / "poll_history.json"
        self.contacts_path = base / "contacts.json"
        self.service = FakeService()
        wa.save_schedule(
            self.schedule_path,
            {
                "enabled": True,
                "weekday": 2,
                "time": "18:00",
                "timezone": "Europe/Berlin",
                "group": "AH Training",
                "skip_dates": [],
                "extra_dates": [],
            },
        )
        ctx = {
            "polls_path": self.polls_path,
            "poll_state_path": self.poll_state,
            "qr_path": self.qr_path,
            "schedule_path": self.schedule_path,
            "schedule_state_path": self.schedule_state,
            "parse_polls": wa.parse_polls,
            "load_schedule": wa.load_schedule,
            "save_schedule": wa.save_schedule,
            "normalize_schedule": wa.normalize_schedule,
            "schedule_slots": wa.schedule_slots,
            "next_schedule_slots": wa.next_schedule_slots,
            "send_next_poll": wa.send_next_poll,
            "choose_poll": wa.choose_poll,
            "load_poll_state": wa.load_poll_state,
            "load_schedule_state": wa.load_schedule_state,
            "poll_history_path": self.poll_history_path,
            "load_poll_history": wa.load_poll_history,
            "contacts_path": self.contacts_path,
            "load_contacts": wa.load_contacts,
            "save_contacts": wa.save_contacts,
            "group_check": lambda query: {
                "available": True,
                "exists": query == "AH Training",
                "name": "AH Training",
                "jid": "1@g.us",
            },
            "poll_history_store": wa.PollHistoryStore(self.poll_history_path),
            "default_group": "AH Training",
            "weekdays": wa.WEEKDAYS_DE,
        }
        self.ctx = ctx
        self.app = create_app(self.service, ctx)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_pages_render(self):
        for path in (
            "/",
            "/polls",
            "/schedule",
            "/votes",
            "/contacts",
            "/api/status",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def _write_history(self):
        store = wa.PollHistoryStore(self.poll_history_path)
        store.record_sent_poll(
            message_id="poll-1",
            chat="1@g.us",
            group_name="AH Training",
            question="Dabei?",
            options=["Ja", "Nein"],
            timestamp=1_700_000_000,
        )
        store.record_vote(
            poll_id="poll-1",
            chat="1@g.us",
            sender="491700000001@s.whatsapp.net",
            sender_alt="",
            pushname="Anna",
            selected_hashes=[wa.hashlib.sha256(b"Ja").hexdigest()],
            timestamp=1_700_000_100,
        )
        store.record_vote(
            poll_id="poll-1",
            chat="1@g.us",
            sender="491700000002@s.whatsapp.net",
            sender_alt="",
            pushname="Bert",
            selected_hashes=[wa.hashlib.sha256(b"Nein").hexdigest()],
            timestamp=1_700_000_200,
        )

    def test_votes_page_shows_names_and_answers(self):
        self._write_history()
        wa.save_contacts(
            self.contacts_path, {"491700000001": "Anna Beispiel"}
        )
        response = self.client.get("/votes")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Dabei?", html)
        self.assertIn("Anna Beispiel", html)
        self.assertIn("Bert", html)
        self.assertIn("Ja", html)
        self.assertIn("Nein", html)

    def test_votes_export_lists_only_names_without_header(self):
        self._write_history()
        response = self.client.get("/votes/export.csv")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True).lstrip("\ufeff")
        self.assertEqual(body, "Anna\nBert\n")

    def test_votes_filter_by_answer(self):
        self._write_history()

        page = self.client.get("/votes?a=0")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Anna", html)
        self.assertNotIn("Bert", html)
        export = (
            self.client.get("/votes/export.csv?a=0")
            .get_data(as_text=True)
            .lstrip("\ufeff")
        )
        self.assertEqual(export, "Anna\n")

        page = self.client.get("/votes?a=1")
        html = page.get_data(as_text=True)
        self.assertIn("Bert", html)
        self.assertNotIn("Anna", html)

    def test_contacts_page_shows_whatsapp_name_under_number(self):
        self._write_history()
        html = self.client.get("/contacts").get_data(as_text=True)
        self.assertIn("contact-whatsapp", html)
        self.assertIn(">Anna<", html)
        self.assertIn(">Bert<", html)

    def test_invalid_answer_filter_shows_all(self):
        self._write_history()
        page = self.client.get("/votes?a=99")
        html = page.get_data(as_text=True)
        self.assertIn("Anna", html)
        self.assertIn("Bert", html)

    def _gate(self):
        from auth import AuthGate, OIDCConfig

        class Validator:
            def validate(self, _token, audience=None):
                self.audience = audience
                return {"sub": "token-user", "exp": time.time() + 3600}

        config = OIDCConfig(
            issuer="https://auth.example.com",
            audience="whatsapp-agent",
            client_id="whatsapp-agent",
        )
        return AuthGate(config, Validator())

    def test_api_requires_bearer_token(self):
        client = create_app(
            self.service, self.ctx, self._gate()
        ).test_client()

        response = client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])

        page = client.get("/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/login", page.headers["Location"])

    def test_api_accepts_valid_bearer_token(self):
        client = create_app(
            self.service, self.ctx, self._gate()
        ).test_client()
        response = client.get(
            "/api/status",
            headers={"Authorization": "Bearer good-token"},
        )
        self.assertEqual(response.status_code, 200)

    def test_api_ignores_session(self):
        client = create_app(
            self.service, self.ctx, self._gate()
        ).test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"sub": "session-user", "exp": time.time() + 3600}
        # API is bearer-only, so a session alone must not authorize it.
        self.assertEqual(client.get("/api/status").status_code, 401)
        # Pages accept the session.
        self.assertEqual(client.get("/").status_code, 200)

    def test_invalid_token_returns_401(self):
        from auth import AuthGate, InvalidToken, OIDCConfig

        class Validator:
            def validate(self, _token, audience=None):
                raise InvalidToken("bad signature")

        config = OIDCConfig(
            issuer="https://auth.example.com",
            audience="whatsapp-agent",
            client_id="whatsapp-agent",
        )
        client = create_app(
            self.service, self.ctx, AuthGate(config, Validator())
        ).test_client()
        response = client.get(
            "/api/status",
            headers={"Authorization": "Bearer bogus"},
        )
        self.assertEqual(response.status_code, 401)

    def test_static_and_health_are_exempt_from_auth(self):
        client = create_app(
            self.service, self.ctx, self._gate()
        ).test_client()
        self.assertEqual(
            client.get("/static/style.css").status_code, 200
        )
        self.assertEqual(client.get("/health").status_code, 200)

    def test_unauthenticated_page_redirects_to_login(self):
        client = create_app(
            self.service, self.ctx, self._gate()
        ).test_client()
        response = client.get("/votes")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/login"))

    def test_login_redirects_to_provider_with_state(self):
        from auth import AuthGate, OIDCConfig

        class FakeLogin:
            def authorization_url(self, **kwargs):
                return (
                    "https://auth.example.com/authorize?state="
                    + kwargs["state"]
                )

            def logout_url(self, **_kwargs):
                return ""

        config = OIDCConfig(
            issuer="https://auth.example.com",
            audience="whatsapp-agent",
            client_id="whatsapp-agent",
        )

        class Validator:
            def validate(self, _token, audience=None):
                return {}

        gate = AuthGate(config, Validator(), FakeLogin())
        client = create_app(self.service, self.ctx, gate).test_client()

        response = client.get("/login")
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(
            location.startswith("https://auth.example.com/authorize?state=")
        )
        with client.session_transaction() as sess:
            self.assertTrue(sess.get("_oidc_state"))
            self.assertTrue(sess.get("_oidc_verifier"))
            self.assertTrue(sess.get("_oidc_nonce"))

    def test_group_check_endpoint(self):
        response = self.client.get(
            "/group-check", query_string={"group": "AH Training"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["available"])
        self.assertTrue(data["exists"])
        self.assertEqual(data["name"], "AH Training")

        missing = self.client.get(
            "/group-check", query_string={"group": "Nope"}
        ).get_json()
        self.assertTrue(missing["available"])
        self.assertFalse(missing["exists"])

        self.service.connected.clear()
        offline = self.client.get(
            "/group-check", query_string={"group": "AH Training"}
        ).get_json()
        self.assertFalse(offline["available"])

    def test_simulate_creates_poll_with_random_votes(self):
        response = self.client.post("/simulate")
        self.assertEqual(response.status_code, 302)
        polls = wa.load_poll_history(self.poll_history_path)["polls"]
        self.assertEqual(len(polls), 1)
        poll = polls[0]
        self.assertTrue(poll.get("simulated"))
        self.assertGreaterEqual(len(poll["votes"]), 6)
        for vote in poll["votes"].values():
            self.assertTrue(vote["selected"])
        page = self.client.get("/votes")
        self.assertEqual(page.status_code, 200)

    def test_clear_simulation_keeps_real_polls(self):
        store = wa.PollHistoryStore(self.poll_history_path)
        store.record_sent_poll(
            message_id="real",
            chat="1@g.us",
            group_name="Echt",
            question="Echt?",
            options=["Ja", "Nein"],
            timestamp=1,
        )
        self.client.post("/simulate")
        self.assertEqual(
            len(wa.load_poll_history(self.poll_history_path)["polls"]), 2
        )
        response = self.client.post("/simulate/clear")
        self.assertEqual(response.status_code, 302)
        polls = wa.load_poll_history(self.poll_history_path)["polls"]
        self.assertEqual([poll["message_id"] for poll in polls], ["real"])

    def test_simulation_buttons_show_while_disconnected(self):
        self.service.connected.clear()
        page = self.client.get("/")
        html = page.get_data(as_text=True)
        self.assertIn("Simulationsdaten hinzufügen", html)
        self.assertNotIn("Simulationsdaten löschen", html)

        self.client.post("/simulate")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Simulationsdaten löschen", html)

    def test_contacts_mapping_is_saved(self):
        response = self.client.post(
            "/contacts",
            data={
                "number": ["491700000001", "491700000002", ""],
                "name": ["Anna", "Bert", "Ignoriert"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            wa.load_contacts(self.contacts_path),
            {"491700000001": "Anna", "491700000002": "Bert"},
        )

    def test_contacts_textarea_is_still_accepted(self):
        response = self.client.post(
            "/contacts",
            data={"contacts": "491700000001 = Anna\n# Kommentar\n"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            wa.load_contacts(self.contacts_path), {"491700000001": "Anna"}
        )

    def test_qr_code_is_only_served_while_disconnected(self):
        self.qr_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        response = self.client.get("/qr.png")
        self.assertEqual(response.status_code, 404)

        self.service.connected.clear()
        response = self.client.get("/qr.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertEqual(response.data, self.qr_path.read_bytes())
        response.close()

    def test_save_polls(self):
        response = self.client.post(
            "/polls",
            data={"polls": "Neu? | Ja | Nein\n"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.polls_path.read_text(encoding="utf-8"), "Neu? | Ja | Nein\n"
        )

    def test_invalid_polls_are_rejected(self):
        before = self.polls_path.read_text(encoding="utf-8")
        response = self.client.post(
            "/polls", data={"polls": "Kaputt | nur eine Antwort\n"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.polls_path.read_text(encoding="utf-8"), before
        )

    def test_schedule_save_and_toggle(self):
        response = self.client.post(
            "/schedule",
            data={
                "enabled": "on",
                "weekday": "3",
                "time": "19:30",
                "timezone": "Europe/Berlin",
                "group": "Neue Gruppe",
                "extra_dates": "2026-12-30\n2026-12-31",
                "month": "2026-12",
            },
        )
        self.assertEqual(response.status_code, 302)
        schedule = wa.load_schedule(self.schedule_path)
        self.assertEqual(schedule["weekday"], 3)
        self.assertEqual(schedule["time"], "19:30")
        self.assertEqual(schedule["group"], "Neue Gruppe")
        self.assertEqual(
            schedule["extra_dates"], ["2026-12-30", "2026-12-31"]
        )
        page = self.client.get("/schedule?month=2026-12")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("2026-12-30", html)
        self.assertIn("2026-12-31", html)

        self.client.post(
            "/schedule/toggle", data={"date": "2026-12-23", "month": "2026-12"}
        )
        schedule = wa.load_schedule(self.schedule_path)
        self.assertEqual(schedule["skip_dates"], ["2026-12-23"])

        self.client.post(
            "/schedule/clear-skips", data={"month": "2026-12"}
        )
        schedule = wa.load_schedule(self.schedule_path)
        self.assertEqual(schedule["skip_dates"], [])

    def test_send_now_sends_and_advances_rotation(self):
        response = self.client.post("/send-now")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.service.sent), 1)
        self.assertEqual(self.service.sent[0]["question"], "Erste?")
        state = wa.load_poll_state(self.poll_state)
        self.assertEqual(state["last_index"], 0)

    def test_send_now_requires_connection(self):
        self.service.connected.clear()
        self.client.post("/send-now")
        self.assertEqual(self.service.sent, [])


if __name__ == "__main__":
    unittest.main()
