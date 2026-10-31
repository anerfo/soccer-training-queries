import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wa
from wa import (
    choose_poll,
    cmd_poll_next,
    match_group,
    normalize_poll,
    normalize_text,
    parse_polls,
)

GROUPS = [
    {"name": "Soccer Mates", "jid": "111-222@g.us"},
    {"name": "Soccer Mates Old", "jid": "333-444@g.us"},
    {"name": "Family", "jid": "555-666@g.us"},
]


class MatchGroupTests(unittest.TestCase):
    def test_exact_name(self):
        self.assertEqual(match_group(GROUPS, "Family")["jid"], "555-666@g.us")

    def test_case_insensitive_exact_name(self):
        self.assertEqual(match_group(GROUPS, "family")["jid"], "555-666@g.us")

    def test_partial_name(self):
        self.assertEqual(match_group(GROUPS, "Old")["jid"], "333-444@g.us")

    def test_jid_passthrough(self):
        self.assertEqual(match_group(GROUPS, "999-000@g.us")["jid"], "999-000@g.us")

    def test_ambiguous_name_raises(self):
        with self.assertRaises(LookupError):
            match_group(GROUPS, "Soccer")

    def test_unknown_name_raises(self):
        with self.assertRaises(LookupError):
            match_group(GROUPS, "Tennis")

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            match_group(GROUPS, "  ")


class NormalizePollTests(unittest.TestCase):
    def test_trims_question_and_options(self):
        question, options = normalize_poll("  Coming?  ", [" Yes ", " No "])
        self.assertEqual(question, "Coming?")
        self.assertEqual(options, ["Yes", "No"])

    def test_needs_two_options(self):
        with self.assertRaises(ValueError):
            normalize_poll("Coming?", ["Yes"])

    def test_rejects_empty_option(self):
        with self.assertRaises(ValueError):
            normalize_poll("Coming?", ["Yes", "  "])

    def test_rejects_duplicate_options(self):
        with self.assertRaises(ValueError):
            normalize_poll("Coming?", ["Yes", "yes"])

    def test_rejects_too_many_options(self):
        with self.assertRaises(ValueError):
            normalize_poll("Coming?", [f"Option {i}" for i in range(13)])

    def test_rejects_empty_question(self):
        with self.assertRaises(ValueError):
            normalize_poll("  ", ["Yes", "No"])


class ParsePollListTests(unittest.TestCase):
    def test_parses_comments_blank_lines_and_whitespace(self):
        polls = parse_polls(
            "# comment\n\n  Coming? | Yes | No  \n# another\nIn? | In | Out\n"
        )
        self.assertEqual(
            polls,
            [
                {"question": "Coming?", "options": ["Yes", "No"]},
                {"question": "In?", "options": ["In", "Out"]},
            ],
        )

    def test_rejects_line_with_too_few_parts(self):
        with self.assertRaises(ValueError):
            parse_polls("Coming? | Yes\n")

    def test_rejects_empty_list(self):
        with self.assertRaises(ValueError):
            parse_polls("# only comments\n\n")

    def test_rejects_invalid_poll(self):
        with self.assertRaises(ValueError):
            parse_polls("Coming? | Yes | yes\n")


class ChoosePollTests(unittest.TestCase):
    def setUp(self):
        self.polls = [
            {"question": "One?", "options": ["Yes", "No"]},
            {"question": "Two?", "options": ["Yes", "No"]},
            {"question": "Three?", "options": ["Yes", "No"]},
        ]

    def test_starts_at_first(self):
        index, poll = choose_poll(self.polls, {})
        self.assertEqual(index, 0)
        self.assertEqual(poll["question"], "One?")

    def test_advances_in_order(self):
        index, poll = choose_poll(self.polls, {"last_index": 0})
        self.assertEqual(index, 1)
        self.assertEqual(poll["question"], "Two?")

    def test_wraps_around(self):
        index, _poll = choose_poll(self.polls, {"last_index": 2})
        self.assertEqual(index, 0)

    def test_ignores_out_of_range_state(self):
        index, _poll = choose_poll(self.polls, {"last_index": 99})
        self.assertEqual(index, 0)

    def test_random_skips_last_index(self):
        with mock.patch("wa.random.choice", return_value=2) as choice:
            index, _poll = choose_poll(self.polls, {"last_index": 0}, True)
        self.assertEqual(index, 2)
        self.assertEqual(choice.call_args.args[0], [1, 2])

    def test_random_with_single_poll(self):
        index, _poll = choose_poll(self.polls[:1], {"last_index": 0}, True)
        self.assertEqual(index, 0)


class PollNextCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.poll_file = base / "polls.txt"
        self.poll_file.write_text(
            "One? | Yes | No\nTwo? | Yes | No\n", encoding="utf-8"
        )
        self.state_file = base / "state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, dry_run=False):
        args = argparse.Namespace(
            group="Group",
            file=str(self.poll_file),
            state=str(self.state_file),
            random=False,
            multiple=False,
            dry_run=dry_run,
            timeout=1.0,
            socket="/tmp/unused.sock",
        )
        response = {
            "ok": True,
            "dry_run": dry_run,
            "name": "Group",
            "jid": "1@g.us",
            "message_id": "id",
        }
        with mock.patch("wa._request", return_value=response):
            return cmd_poll_next(args)

    def test_advances_through_list_and_wraps(self):
        self.assertEqual(self._run(), 0)
        self.assertIn('"last_index": 0', self.state_file.read_text())
        self.assertEqual(self._run(), 0)
        self.assertIn('"last_index": 1', self.state_file.read_text())
        self.assertEqual(self._run(), 0)
        self.assertIn('"last_index": 0', self.state_file.read_text())

    def test_dry_run_does_not_advance(self):
        self.assertEqual(self._run(dry_run=True), 0)
        self.assertFalse(self.state_file.exists())

    def test_failed_send_does_not_advance(self):
        with mock.patch("wa._request", return_value={"ok": False, "error": "nope"}):
            args = argparse.Namespace(
                group="Group",
                file=str(self.poll_file),
                state=str(self.state_file),
                random=False,
                multiple=False,
                dry_run=False,
                timeout=1.0,
                socket="/tmp/unused.sock",
            )
            self.assertEqual(cmd_poll_next(args), 1)
        self.assertFalse(self.state_file.exists())


class NormalizeTextTests(unittest.TestCase):
    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            normalize_text("   ")

    def test_long_text_raises(self):
        with self.assertRaises(ValueError):
            normalize_text("x" * 65_537)

    def test_text_is_preserved(self):
        self.assertEqual(normalize_text("  hello\n"), "  hello\n")


class PollVoteWorkaroundTests(unittest.TestCase):
    def test_errcheck_converts_bytes_struct_and_frees_it(self):
        import ctypes

        from neonize import _binder

        class FakeFunc:
            errcheck = None

        class FakeGoCode:
            def __init__(self):
                self.DecryptPollVote = FakeFunc()
                self.freed = []
                self.FreeBytesStruct = self.freed.append

        fake = FakeGoCode()
        with mock.patch.object(_binder, "gocode", fake):
            wa._install_poll_vote_workaround()

            buffer = ctypes.create_string_buffer(b"hello")
            contents = mock.Mock()
            contents.ptr = ctypes.addressof(buffer)
            contents.size = 5
            result = mock.Mock()
            result.contents = contents

            self.assertEqual(
                fake.DecryptPollVote.errcheck(result, None, None), b"hello"
            )
            self.assertEqual(fake.freed, [result])
            self.assertEqual(fake.DecryptPollVote.errcheck(None, None, None), b"")


class PollHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "history.json"
        self.store = wa.PollHistoryStore(self.history_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_vote_hashes_are_mapped_to_option_names(self):
        self.store.record_sent_poll(
            message_id="poll-1",
            chat="1@g.us",
            group_name="Gruppe",
            question="Dabei?",
            options=["Ja", "Nein"],
            timestamp=100,
        )
        ja_hash = wa.hashlib.sha256(b"Ja").hexdigest()
        nein_hash = wa.hashlib.sha256(b"Nein").hexdigest()
        self.assertTrue(
            self.store.record_vote(
                poll_id="poll-1",
                chat="1@g.us",
                sender="491700000001@s.whatsapp.net",
                sender_alt="",
                pushname="Anna",
                selected_hashes=[ja_hash],
                timestamp=101,
            )
        )
        self.store.record_vote(
            poll_id="poll-1",
            chat="1@g.us",
            sender="491700000002@s.whatsapp.net",
            sender_alt="",
            pushname="Bert",
            selected_hashes=[nein_hash],
            timestamp=102,
        )
        poll = wa.load_poll_history(self.history_path)["polls"][0]
        votes = poll["votes"]
        self.assertEqual(votes["491700000001@s.whatsapp.net"]["selected"], ["Ja"])
        self.assertEqual(votes["491700000002@s.whatsapp.net"]["selected"], ["Nein"])

    def test_timestamps_are_normalized_to_seconds(self):
        self.assertEqual(wa.normalize_epoch(1_793_306_491_000), 1_793_306_491)
        self.assertEqual(wa.normalize_epoch(1_793_306_491_000_000), 1_793_306_491)
        self.assertEqual(wa.normalize_epoch(1_793_306_491), 1_793_306_491)
        self.assertEqual(wa.normalize_epoch(0), 0)
        self.assertEqual(wa.normalize_epoch("nope"), 0)

        self.store.record_sent_poll(
            message_id="poll-ms",
            chat="1@g.us",
            group_name="Gruppe",
            question="Dabei?",
            options=["Ja", "Nein"],
            timestamp=1_793_306_491_000,
        )
        self.store.record_vote(
            poll_id="poll-ms",
            chat="1@g.us",
            sender="1@s.whatsapp.net",
            sender_alt="",
            pushname="Anna",
            selected_hashes=[wa.hashlib.sha256(b"Ja").hexdigest()],
            timestamp=1_793_306_491_000,
        )
        poll = wa.load_poll_history(self.history_path)["polls"][0]
        self.assertEqual(poll["sent_at"], 1_793_306_491)
        self.assertEqual(poll["votes"]["1@s.whatsapp.net"]["timestamp"], 1_793_306_491)

    def test_delete_poll_and_clear_all(self):
        for message_id in ("a", "b"):
            self.store.record_sent_poll(
                message_id=message_id,
                chat="1@g.us",
                group_name="Gruppe",
                question="Dabei?",
                options=["Ja", "Nein"],
                timestamp=100,
            )
        self.assertTrue(self.store.delete_poll("a"))
        self.assertFalse(self.store.delete_poll("missing"))
        polls = wa.load_poll_history(self.history_path)["polls"]
        self.assertEqual([poll["message_id"] for poll in polls], ["b"])

        self.assertEqual(self.store.clear_all(), 1)
        self.assertEqual(wa.load_poll_history(self.history_path)["polls"], [])
        self.assertEqual(self.store.clear_all(), 0)

    def test_unknown_poll_vote_is_ignored(self):
        self.assertFalse(
            self.store.record_vote(
                poll_id="missing",
                chat="1@g.us",
                sender="1@s.whatsapp.net",
                sender_alt="",
                pushname="X",
                selected_hashes=[],
                timestamp=1,
            )
        )


class GroupContactsTests(unittest.TestCase):
    def setUp(self):
        from neonize.proto import Neonize_pb2 as neonize_proto

        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)

        def jid(user, server):
            return neonize_proto.JID(User=user, Server=server, IsEmpty=False)

        group = neonize_proto.GroupInfo()
        group.JID.CopyFrom(jid("120363", "g.us"))
        group.GroupName.Name = "Gruppe"
        first = group.Participants.add()
        first.JID.CopyFrom(jid("491700000001", "s.whatsapp.net"))
        first.PhoneNumber.CopyFrom(jid("491700000001", "s.whatsapp.net"))
        first.DisplayName = "Anna"
        second = group.Participants.add()
        second.JID.CopyFrom(jid("491700000002", "s.whatsapp.net"))
        second.PhoneNumber.CopyFrom(jid("491700000002", "s.whatsapp.net"))
        second.DisplayName = "Bert"

        contact = neonize_proto.Contact(JID=jid("491700000001", "s.whatsapp.net"))
        contact.Info.FullName = "Anna Beispiel"

        self.group = group

        class FakeClient:
            def __init__(self, group, contacts):
                self.group = group
                self.contact = mock.MagicMock()
                self.contact.get_all_contacts.return_value = contacts

            def get_joined_groups(self):
                return [self.group]

            def get_group_info(self, _jid):
                return self.group

        self.service = wa.WhatsAppService(
            base / "wa.db", base / "qr.png", base / "history.json"
        )
        self.service.connected.set()
        self.service.client = FakeClient(group, [contact])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_names_come_from_phonebook_then_display_name(self):
        contacts = self.service.group_contacts("Gruppe")
        self.assertEqual(contacts["491700000001"], "Anna Beispiel")
        self.assertEqual(contacts["491700000002"], "Bert")

    def test_directory_keeps_whatsapp_name(self):
        directory = self.service.group_directory("Gruppe")
        self.assertEqual(directory["491700000001"]["name"], "Anna Beispiel")
        self.assertEqual(directory["491700000001"]["whatsapp"], "Anna")
        self.assertEqual(directory["491700000002"]["name"], "Bert")
        self.assertEqual(directory["491700000002"]["whatsapp"], "Bert")

    def test_group_check_existing_name_and_jid(self):
        by_name = self.service.group_check("Gruppe")
        self.assertTrue(by_name["available"])
        self.assertTrue(by_name["exists"])
        self.assertEqual(by_name["name"], "Gruppe")

        by_jid = self.service.group_check("120363@g.us")
        self.assertTrue(by_jid["exists"])
        self.assertEqual(by_jid["jid"], "120363@g.us")

    def test_group_check_unknown_group(self):
        result = self.service.group_check("Gibt es nicht")
        self.assertTrue(result["available"])
        self.assertFalse(result["exists"])

    def test_group_check_unavailable_when_disconnected(self):
        self.service.connected.clear()
        result = self.service.group_check("Gruppe")
        self.assertFalse(result["available"])
        self.assertFalse(result["exists"])


class HandlePollVoteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.history_path = base / "history.json"
        self.service = wa.WhatsAppService(
            base / "wa.db", base / "qr.png", self.history_path
        )
        self.service.history.record_sent_poll(
            message_id="poll-1",
            chat="120363@g.us",
            group_name="Gruppe",
            question="Dabei?",
            options=["Ja", "Nein"],
            timestamp=100,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _event(self):
        from neonize.proto import Neonize_pb2 as neonize_proto

        event = neonize_proto.Message()
        event.Info.ID = "vote-msg"
        event.Info.Pushname = "Anna"
        event.Info.Timestamp = 200
        source = event.Info.MessageSource
        source.Chat.User = "120363"
        source.Chat.Server = "g.us"
        source.Sender.User = "491700000001"
        source.Sender.Server = "s.whatsapp.net"
        event.Message.pollUpdateMessage.pollCreationMessageKey.ID = "poll-1"
        return event

    def test_poll_vote_is_recorded(self):
        from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as e2e

        vote = e2e.PollVoteMessage(selectedOptions=[wa.hashlib.sha256(b"Ja").digest()])
        self.service.client = mock.MagicMock()
        self.service.client.decrypt_poll_vote.return_value = vote

        self.service._handle_message(self._event())

        poll = wa.load_poll_history(self.history_path)["polls"][0]
        vote_entry = poll["votes"]["491700000001@s.whatsapp.net"]
        self.assertEqual(vote_entry["selected"], ["Ja"])
        self.assertEqual(vote_entry["pushname"], "Anna")


if __name__ == "__main__":
    unittest.main()
