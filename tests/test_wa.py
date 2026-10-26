import unittest
from unittest import mock

from wa import (
    choose_poll,
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
        self.assertEqual(
            match_group(GROUPS, "Old")["jid"], "333-444@g.us"
        )

    def test_jid_passthrough(self):
        self.assertEqual(
            match_group(GROUPS, "999-000@g.us")["jid"], "999-000@g.us"
        )

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


class NormalizeTextTests(unittest.TestCase):
    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            normalize_text("   ")

    def test_long_text_raises(self):
        with self.assertRaises(ValueError):
            normalize_text("x" * 65_537)

    def test_text_is_preserved(self):
        self.assertEqual(normalize_text("  hello\n"), "  hello\n")


if __name__ == "__main__":
    unittest.main()
