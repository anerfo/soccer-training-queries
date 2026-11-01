import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import soccer.calendar as calendar_sync
from soccer import daemon as wa
from soccer.calendar import data as calendar_data

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:series-1
DTSTAMP:20261001T000000Z
DTSTART:20261007T190000Z
DTEND:20261007T203000Z
RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=6
SUMMARY:AH-Training
EXDATE:20261021T190000Z
END:VEVENT
END:VCALENDAR
"""


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class FakeService:
    def __init__(self, connected=True):
        self.connected = threading.Event()
        if connected:
            self.connected.set()
        self.polls = []
        self.texts = []

    def poll(self, group, question, options, multiple=False, dry_run=False):
        self.polls.append((group, question, options))
        return {"ok": True}

    def send(self, group, text, dry_run=False):
        self.texts.append((group, text))
        return {"ok": True}


class FetchTests(unittest.TestCase):
    class Response:
        def __init__(self, body, status=200):
            self.content = body
            self.status_code = status
            self.headers = {}

    def test_html_response_is_rejected_with_short_error(self):
        response = self.Response(b"<!doctype html><html>sign in</html>")
        with (
            mock.patch("soccer.calendar.data.validate_calendar_url"),
            mock.patch("soccer.calendar.data.requests.get", return_value=response),
        ):
            with self.assertRaises(calendar_sync.CalendarError) as ctx:
                calendar_sync.fetch_ics("https://example.com/cal")
        message = str(ctx.exception)
        self.assertIn("iCalendar", message)
        self.assertLess(len(message), 300)

    def test_error_is_cached(self):
        cache = calendar_sync.CalendarFeedCache()
        calls = {"count": 0}

        def fail(_url):
            calls["count"] += 1
            raise calendar_sync.CalendarError("boom")

        with mock.patch("soccer.calendar.data.fetch_ics", side_effect=fail):
            for _ in range(3):
                with self.assertRaises(calendar_sync.CalendarError):
                    cache.get("https://example.com/cal")
        self.assertEqual(calls["count"], 1)


class CalendarUrlValidationTests(unittest.TestCase):
    def test_rejects_private_loopback_and_metadata_addresses(self):
        for url in (
            "http://127.0.0.1/cal.ics",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/cal.ics",
            "ftp://example.com/cal.ics",
        ):
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_data.validate_calendar_url(url)

    def test_allows_private_when_explicitly_enabled(self):
        with mock.patch.object(calendar_data, "ALLOW_PRIVATE_CALENDARS", True):
            calendar_data.validate_calendar_url("http://127.0.0.1/cal.ics")

    def test_allows_public_ip_literals(self):
        calendar_data.validate_calendar_url("https://1.1.1.1/cal.ics")


class OffsetTests(unittest.TestCase):
    def test_offsets(self):
        self.assertEqual(calendar_sync.parse_offset("20h"), 1200)
        self.assertEqual(calendar_sync.parse_offset("2d"), 2880)
        self.assertEqual(calendar_sync.parse_offset("90m"), 90)
        self.assertEqual(calendar_sync.parse_offset("1,5h"), 90)

    def test_invalid_offset(self):
        with self.assertRaises(calendar_sync.CalendarError):
            calendar_sync.parse_offset("später")


class MatchTests(unittest.TestCase):
    def test_contains_exact_regex(self):
        rule = {"match": "training", "match_type": "contains"}
        self.assertTrue(calendar_sync.match_event(rule, "AH-Training am Mittwoch"))
        self.assertFalse(calendar_sync.match_event(rule, "Spieltag"))

        rule = {"match": "AH-Training", "match_type": "exact"}
        self.assertTrue(calendar_sync.match_event(rule, "AH-Training"))
        self.assertFalse(calendar_sync.match_event(rule, "AH-Training 2"))

        rule = {"match": r"^AH-.*$", "match_type": "regex"}
        self.assertTrue(calendar_sync.match_event(rule, "AH-Training"))


class ExpandTests(unittest.TestCase):
    def test_recurrence_and_exdate(self):
        events = calendar_sync.expand_events(ICS, utc(2026, 10, 1), utc(2026, 11, 15))
        starts = [event["start"] for event in events]
        self.assertEqual(
            starts,
            [
                utc(2026, 10, 7, 19),
                utc(2026, 10, 14, 19),
                utc(2026, 10, 28, 19),
                utc(2026, 11, 4, 19),
                utc(2026, 11, 11, 19),
            ],
        )

    def test_cancelled_override_is_skipped(self):
        ics = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:event-1
DTSTAMP:20261001T000000Z
DTSTART:20261007T190000Z
SUMMARY:Training
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""
        events = calendar_sync.expand_events(ics, utc(2026, 10, 1), utc(2026, 10, 15))
        self.assertEqual(events, [])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.config_path = base / "calendars.json"
        self.state_path = base / "calendar_state.json"
        self.polls_path = base / "polls.txt"
        self.polls_path.write_text(
            "Erste? | Ja | Nein\nZweite? | Ja | Nein\n", encoding="utf-8"
        )
        self.service = FakeService()
        self.config_path.write_text(
            json.dumps(
                {
                    "calendars": [
                        {
                            "id": "cal1",
                            "name": "Test",
                            "url": "test://ics",
                            "enabled": True,
                        }
                    ],
                    "rules": [
                        {
                            "id": "rule1",
                            "calendar_id": "cal1",
                            "match": "AH-Training",
                            "match_type": "contains",
                            "group": "Gruppe",
                            "kind": "poll",
                            "text": "",
                            "poll_questions": ["Erste?", "Zweite?"],
                            "offset": "20h",
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.scheduler = calendar_sync.CalendarScheduler(
            self.service,
            self.config_path,
            self.state_path,
            self.polls_path,
            wa.parse_polls,
        )
        self.patch = mock.patch("soccer.calendar.data.fetch_ics", return_value=ICS)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_sends_once_and_rotates(self):
        self.scheduler.tick(now=utc(2026, 10, 6, 23))
        self.assertEqual(len(self.service.polls), 1)
        self.assertEqual(self.service.polls[0][1], "Erste?")

        self.scheduler.tick(now=utc(2026, 10, 6, 23, 5))
        self.assertEqual(len(self.service.polls), 1)

        self.scheduler.tick(now=utc(2026, 10, 13, 23))
        self.assertEqual(len(self.service.polls), 2)
        self.assertEqual(self.service.polls[1][1], "Zweite?")

    def test_deleted_occurrence_is_not_sent(self):
        self.scheduler.tick(now=utc(2026, 10, 20, 23))
        self.assertEqual(self.service.polls, [])

    def test_not_connected_does_not_send_or_mark(self):
        self.service.connected.clear()
        self.scheduler.tick(now=utc(2026, 10, 6, 23))
        self.assertEqual(self.service.polls, [])
        state = calendar_sync.load_calendar_state(self.state_path)
        self.assertEqual(state["sent"], {})

    def test_text_rule(self):
        config = calendar_sync.load_calendar_config(self.config_path)
        config["rules"][0].update(
            {"kind": "text", "text": "Bitte abstimmen", "offset": "1h"}
        )
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.scheduler.tick(now=utc(2026, 10, 7, 18))
        self.assertEqual(self.service.texts, [("Gruppe", "Bitte abstimmen")])

    def test_month_entries_show_events_and_sends(self):
        data, error = self.scheduler.month_entries(
            2026, 11, "UTC", now=utc(2026, 10, 1)
        )
        self.assertIsNone(error)
        self.assertEqual(
            [event["summary"] for event in data["events"]],
            ["AH-Training", "AH-Training"],
        )
        self.assertEqual(
            [event["start"].date().isoformat() for event in data["events"]],
            ["2026-11-04", "2026-11-11"],
        )
        self.assertEqual(len(data["sends"]), 2)
        self.assertEqual(
            [send["content"] for send in data["sends"]],
            ["Erste?", "Zweite?"],
        )
        self.assertEqual(data["sends"][0]["send_at"].date().isoformat(), "2026-11-03")

    def test_month_entries_use_history_for_sent_messages(self):
        self.scheduler.tick(now=utc(2026, 11, 3, 23))
        data, error = self.scheduler.month_entries(
            2026, 11, "UTC", now=utc(2026, 11, 4)
        )
        self.assertIsNone(error)
        sent = [send for send in data["sends"] if send["sent"]]
        upcoming = [send for send in data["sends"] if not send["sent"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["content"], "Erste?")
        self.assertEqual(len(upcoming), 1)
        self.assertEqual(upcoming[0]["content"], "Zweite?")

    def test_invalid_offset_is_skipped(self):
        config = calendar_sync.load_calendar_config(self.config_path)
        config["rules"][0]["offset"] = "später"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.scheduler.tick(now=utc(2026, 10, 6, 23))
        self.assertEqual(self.service.polls, [])

    def test_preview_reports_invalid_offset(self):
        config = calendar_sync.load_calendar_config(self.config_path)
        config["rules"][0]["offset"] = "später"
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        items, error = self.scheduler.preview(now=utc(2026, 10, 6, 12))
        self.assertEqual(items, [])
        self.assertIn("offset", error)

    def test_send_planned_sends_previewed_message(self):
        items, error = self.scheduler.preview(now=utc(2026, 10, 6, 12))
        self.assertIsNone(error)
        item = items[0]
        info = self.scheduler.send_planned(item, group="Gruppe")
        self.assertTrue(info["marked"])
        self.assertEqual([poll[1] for poll in self.service.polls], ["Erste?"])

        next_items, _error = self.scheduler.preview(now=utc(2026, 10, 6, 12, 5))
        self.assertEqual(next_items[0]["content"], "Zweite?")
        self.assertNotEqual(next_items[0]["key"], item["key"])

    def test_send_planned_to_other_group_keeps_schedule(self):
        items, _error = self.scheduler.preview(now=utc(2026, 10, 6, 12))
        item = items[0]
        info = self.scheduler.send_planned(item, group="Andere")
        self.assertFalse(info["marked"])
        next_items, _error = self.scheduler.preview(now=utc(2026, 10, 6, 12, 5))
        self.assertEqual(next_items[0]["content"], "Erste?")

    def test_preview_shows_upcoming_sends(self):
        items, error = self.scheduler.preview(now=utc(2026, 10, 6, 12))
        self.assertIsNone(error)
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0]["kind"], "poll")
        self.assertEqual(items[0]["content"], "Erste?")
        self.assertEqual(items[0]["send_at"], utc(2026, 10, 6, 23))
        self.assertEqual(items[1]["content"], "Zweite?")


if __name__ == "__main__":
    unittest.main()
