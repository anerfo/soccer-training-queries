import tempfile
import threading
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
            "load_poll_state": wa.load_poll_state,
            "load_schedule_state": wa.load_schedule_state,
            "default_group": "AH Training",
            "weekdays": wa.WEEKDAYS_DE,
        }
        self.app = create_app(self.service, ctx)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_pages_render(self):
        for path in ("/", "/polls", "/schedule", "/api/status"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

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
