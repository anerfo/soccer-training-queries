import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import calendar_sync
import wa

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
    return datetime(*args, tzinfo=timezone.utc)


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
        self.assertTrue(
            calendar_sync.match_event(rule, "AH-Training am Mittwoch")
        )
        self.assertFalse(calendar_sync.match_event(rule, "Spieltag"))

        rule = {"match": "AH-Training", "match_type": "exact"}
        self.assertTrue(calendar_sync.match_event(rule, "AH-Training"))
        self.assertFalse(calendar_sync.match_event(rule, "AH-Training 2"))

        rule = {"match": r"^AH-.*$", "match_type": "regex"}
        self.assertTrue(calendar_sync.match_event(rule, "AH-Training"))


class ExpandTests(unittest.TestCase):
    def test_recurrence_and_exdate(self):
        events = calendar_sync.expand_events(
            ICS, utc(2026, 10, 1), utc(2026, 11, 15)
        )
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
        events = calendar_sync.expand_events(
            ics, utc(2026, 10, 1), utc(2026, 10, 15)
        )
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
        self.patch = mock.patch(
            "calendar_sync.fetch_ics", return_value=ICS
        )
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
        self.assertEqual(
            self.service.texts, [("Gruppe", "Bitte abstimmen")]
        )

    def test_preview_shows_upcoming_sends(self):
        items, error = self.scheduler.preview(now=utc(2026, 10, 6, 12))
        self.assertIsNone(error)
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0]["kind"], "poll")
        self.assertEqual(items[0]["content"], "Erste?")
        self.assertEqual(items[0]["send_at"], utc(2026, 10, 6, 23))
        self.assertEqual(items[1]["content"], "Zweite?")


class RulesEnabledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "calendars.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_rules_enabled(self):
        self.path.write_text(
            json.dumps(
                {
                    "calendars": [{"id": "c", "enabled": True}],
                    "rules": [{"id": "r", "calendar_id": "c", "enabled": True}],
                }
            )
        )
        self.assertTrue(calendar_sync.calendar_rules_enabled(self.path))

    def test_no_rules(self):
        self.path.write_text(
            json.dumps(
                {
                    "calendars": [{"id": "c", "enabled": True}],
                    "rules": [{"id": "r", "calendar_id": "c", "enabled": False}],
                }
            )
        )
        self.assertFalse(calendar_sync.calendar_rules_enabled(self.path))


if __name__ == "__main__":
    unittest.main()
