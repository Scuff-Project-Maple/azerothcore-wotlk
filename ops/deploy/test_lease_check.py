#!/usr/bin/env python3
"""Tests for the single-writer lease reader (PRO-356).

    python3 ops/deploy/test_lease_check.py

No MySQL, no Docker, no deploy host: every fixture is a file in a temp directory and every
timestamp is built relative to `now`, so the suite cannot rot into a test that passes only
while a particular calendar date is inside a 180-minute window.

The PRO-356 defect was that the documented CHECK called a **live** lease
`LEASE_STALE_BREAKABLE`, because `jq`'s `fromdate` rejects `...T19:23:14.086945Z`, the
predicate raised a jq *error*, and the `||` branch that prints the breakable verdict cannot
tell an error from an expiry. The tests below are therefore written as a classification
contract — LIVE / STALE / UNREADABLE — with a standing rule that nothing malformed may ever
be called breakable, plus two drift guards that fail if the doc or the boot wrapper grows a
timestamp parser of its own again.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lease_check import (  # noqa: E402
    CANONICAL_TIMESTAMP, EXIT_FOR_VERDICT, LEASE_FREE, LEASE_HELD_LIVE, LEASE_STALE_BREAKABLE,
    LEASE_UNREADABLE, classify, main, parse_acquired_at, parse_window,
)

CHECKER = HERE / "lease_check.py"
REPO_ROOT = HERE.parents[1]
DOC = REPO_ROOT / "docs" / "dev-environment.md"
WRAPPER = HERE / "worldserver-up.sh"

UTC = dt.timezone.utc
#: The holder from the PRO-356 report, so a regression reads as the same incident.
AGENT = "0bd1ce89-1e4b-4a75-801c-13005902b199"
ISSUE = "1552639d-ab20-49a4-8341-1c2c1b2468ef"


def now():
    return dt.datetime.now(UTC)


def stamp(**kwargs):
    """An ISO-8601 UTC stamp offset from now, in the shape a real writer produces."""
    moment = now() + dt.timedelta(**kwargs)
    return moment.isoformat().replace("+00:00", "Z")


def lease(microseconds=True, **fields):
    """Lease JSON text, live by default: 28 min into a 180 min window, as in the report."""
    body = {"agentId": AGENT, "issueId": ISSUE,
            "action": "pro344-preflight-record-qa",
            "acquiredAt": stamp(minutes=-28), "expectedMinutes": 180}
    if not microseconds:
        body["acquiredAt"] = body["acquiredAt"].split(".")[0] + "Z"
    body.update(fields)
    return json.dumps(body)


def verdict_of(text):
    return classify(text).verdict


class TestTheReportedDefect(unittest.TestCase):
    """PRO-356: a fractional-second `acquiredAt` is legal, and it was read as breakable."""

    def test_the_reported_lease_classifies_live(self):
        self.assertEqual(LEASE_HELD_LIVE, verdict_of(lease()))

    def test_the_reported_lease_is_still_live_under_verify(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            path.write_text(lease())
            self.assertEqual(0, main([  # verify() exits 0 only for a live lease
                "verify", str(path), "--label", "worldserver-up"]))

    def test_whole_second_stamp_still_classifies_live(self):
        self.assertEqual(LEASE_HELD_LIVE, verdict_of(lease(microseconds=False)))

    def test_the_checker_and_the_wrapper_reach_the_same_verdict(self):
        """The two readers disagreed; one parser is the fix, so prove they are the same call."""
        import tempfile
        cases = [lease(), lease(microseconds=False), lease(acquiredAt=stamp(minutes=-200)),
                 "not json at all", lease(acquiredAt=None)]
        for text in cases:
            with self.subTest(text=text[:48]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "lease"
                    path.write_text(text)
                    from lease_check import assess
                    self.assertEqual(classify(text).verdict, assess(path).verdict)


class TestTimestampTolerance(unittest.TestCase):
    """Every stamp a plausible writer has ever emitted must be readable, not breakable."""

    def test_offset_and_naive_and_odd_fractions_are_all_readable(self):
        base = stamp(minutes=-5)
        whole = base.split(".")[0]
        naive_form = base.replace("Z", "")
        cases = {
            "Zulu": base,
            "explicit offset": base.replace("Z", "+00:00"),
            "naive": naive_form,
            "one-digit fraction": whole + ".5Z",
            "two-digit fraction": whole + ".08Z",
            "nine-digit fraction": whole + ".086945123Z",
            "space separator": base.replace("T", " "),
            "whole second": whole + "Z",
        }
        for name, raw in cases.items():
            with self.subTest(case=name, raw=raw):
                self.assertIsInstance(parse_acquired_at(raw), dt.datetime)
                self.assertEqual(LEASE_HELD_LIVE,
                                 verdict_of(lease(acquiredAt=raw)),
                                 f"{name} was not read as live")

    def test_non_utc_offset_is_measured_against_utc_now(self):
        # 09:00+02:00 is 07:00Z, so a lease taken 5 min ago in a +02:00 zone is live.
        local = now().astimezone(dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual(LEASE_HELD_LIVE, verdict_of(lease(acquiredAt=local.isoformat())))

    def test_a_future_stamp_is_live_not_breakable(self):
        self.assertEqual(LEASE_HELD_LIVE, verdict_of(lease(acquiredAt=stamp(minutes=+30))))


class TestFailClosed(unittest.TestCase):
    """The rule the jq predicate broke: only a *proven* expiry may be called breakable."""

    def assert_never_breakable(self, text, why):
        with self.subTest(why=why, text=text[:48]):
            reading = classify(text)
            self.assertEqual(LEASE_UNREADABLE, reading.verdict,
                             f"{why} was classified {reading.verdict}; only a proven expiry "
                             f"may be LEASE_STALE_BREAKABLE")
            self.assertNotIn("LEASE_STALE_BREAKABLE", json.dumps(reading.notes))

    def test_garbage_and_half_writes(self):
        self.assert_never_breakable("", "empty file (a writer mid-redirect)")
        self.assert_never_breakable("   ", "whitespace only")
        self.assert_never_breakable("{", "truncated JSON")
        self.assert_never_breakable('{"agentId": "a", ', "half an object")
        self.assert_never_breakable("not json at all", "prose")
        self.assert_never_breakable("[]", "an array, not an object")
        self.assert_never_breakable("null", "JSON null")

    def test_no_holder(self):
        self.assert_never_breakable('{"acquiredAt": "%s", "expectedMinutes": 180}'
                                    % stamp(minutes=-28), "no agentId")
        self.assert_never_breakable(lease(agentId=""), "empty agentId")
        self.assert_never_breakable(lease(agentId="   "), "blank agentId")
        self.assert_never_breakable(lease(agentId=42), "numeric agentId")

    def test_unusable_acquired_at(self):
        for raw in (None, "", "   ", "yesterday", "2026-13-45T99:99:99Z", "1730000000",
                    1730000000, "2026-11-07T19:23:14+25:00", "not-a-timestamp"):
            self.assert_never_breakable(lease(acquiredAt=raw), f"acquiredAt={raw!r}")

    def test_unusable_window(self):
        for raw in (None, "abc", "", 0, -30, True, float("nan"), float("inf"), []):
            self.assert_never_breakable(lease(expectedMinutes=raw), f"expectedMinutes={raw!r}")

    def test_a_lease_that_is_malformed_and_expired_is_still_unreadable(self):
        """The fail-open case in one line: past the window *and* an unparsable stamp."""
        self.assert_never_breakable(
            '{"agentId": "%s", "acquiredAt": "2020-01-01T00:00:00Z", "expectedMinutes": "soon"}'
            % AGENT, "expired but unprovable")

    def test_the_cli_refuses_to_print_the_breakable_verdict_for_garbage(self):
        import tempfile
        for text in ("", "{", "not json at all", lease(acquiredAt="yesterday"),
                     lease(expectedMinutes=None)):
            with self.subTest(text=text[:48]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "lease"
                    path.write_text(text)
                    proc = self._check(path)
                    self.assertEqual(EXIT_FOR_VERDICT[LEASE_UNREADABLE], proc.returncode)
                    self.assertIn(LEASE_UNREADABLE, proc.stdout)
                    self.assertNotIn(LEASE_STALE_BREAKABLE, proc.stdout)
                    self.assertIn("do NOT", proc.stdout,
                                  "an unreadable lease has to tell the agent not to rm it")

    def _check(self, path):
        return subprocess.run([sys.executable, str(CHECKER), "check", str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60)


class TestExpiryIsStillBreakable(unittest.TestCase):
    """Fail closed must not mean never breakable — the doc's stale-lease path has to work."""

    def test_past_the_window_is_stale_breakable(self):
        self.assertEqual(LEASE_STALE_BREAKABLE,
                         verdict_of(lease(acquiredAt=stamp(minutes=-200), expectedMinutes=180)))

    def test_fractional_stamp_past_the_window_is_stale_breakable(self):
        """The other half of PRO-356 ask 4: fractional *and* genuinely expired is breakable."""
        raw = stamp(minutes=-300)
        self.assertIn(".", raw, "the fixture lost its fractional seconds")
        self.assertEqual(LEASE_STALE_BREAKABLE, verdict_of(lease(acquiredAt=raw)))

    def test_inside_the_window_is_live(self):
        self.assertEqual(LEASE_HELD_LIVE,
                         verdict_of(lease(acquiredAt=stamp(minutes=-179), expectedMinutes=180)))

    def test_exit_codes_separate_the_three_outcomes(self):
        import tempfile
        cases = {LEASE_HELD_LIVE: lease(),
                 LEASE_STALE_BREAKABLE: lease(acquiredAt=stamp(minutes=-200)),
                 LEASE_UNREADABLE: "garbage"}
        for expected, text in cases.items():
            with self.subTest(verdict=expected):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "lease"
                    path.write_text(text)
                    proc = subprocess.run([sys.executable, str(CHECKER), "check",
                                           "--quiet", str(path)],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, timeout=60)
                    self.assertEqual(EXIT_FOR_VERDICT[expected], proc.returncode)
                    self.assertEqual(expected, proc.stdout.strip())

    def test_no_file_is_free(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            proc = subprocess.run([sys.executable, str(CHECKER), "check", "--quiet", str(path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertEqual(EXIT_FOR_VERDICT[LEASE_FREE], proc.returncode)
            self.assertEqual(LEASE_FREE, proc.stdout.strip())

    def test_a_directory_is_unreadable_not_free(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(CHECKER), "check", "--quiet", tmp],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertEqual(EXIT_FOR_VERDICT[LEASE_UNREADABLE], proc.returncode)


class TestWriterSide(unittest.TestCase):
    """PRO-356 ask 3: the writer and the reader have to agree on the stamp format."""

    def _acquire(self, tmp, *extra):
        return subprocess.run(
            [sys.executable, str(CHECKER), "acquire", str(Path(tmp) / "lease"),
             "--agent", "agent-A", "--issue", "PRO-356", "--action", "e2e-suite", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)

    def test_acquire_pins_the_whole_second_form_the_reader_reads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            proc = self._acquire(tmp)
            self.assertEqual(0, proc.returncode, proc.stdout)
            self.assertIn("LEASE_ACQUIRED", proc.stdout)
            raw = json.loads(path.read_text())
            self.assertRegex(raw["acquiredAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
                             "the writer drifted off the form `date -u +%FT%TZ` produces")
            self.assertEqual(raw["expectedMinutes"], 90)
            self.assertEqual(LEASE_HELD_LIVE, classify(path.read_text()).verdict,
                             "a lease this tool wrote must be readable by this tool")

    def test_acquire_never_overwrites_a_live_lease(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            path.write_text(lease())
            proc = subprocess.run([sys.executable, str(CHECKER), "acquire", str(path),
                                   "--agent", "agent-B", "--issue", "PRO-9", "--action",
                                   "compose-up"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("LEASE_HELD", proc.stdout)
            self.assertIn(AGENT, path.read_text(), "a live holder's lease was replaced")

    def test_release_only_removes_your_own_lease(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            path.write_text(lease())
            wrong = subprocess.run([sys.executable, str(CHECKER), "release", str(path),
                                    "--agent", "someone-else", "--issue", "PRO-1"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, timeout=60)
            self.assertEqual(4, wrong.returncode)
            self.assertIn("LEASE_NOT_YOURS", wrong.stdout)
            self.assertTrue(path.exists(), "release removed a lease it did not hold")

            mine = subprocess.run([sys.executable, str(CHECKER), "release", str(path),
                                   "--agent", AGENT, "--issue",
                                   "1552639d-ab20-49a4-8341-1c2c1b2468ef"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertEqual(0, mine.returncode, mine.stdout)
            self.assertIn("LEASE_RELEASED", mine.stdout)
            self.assertFalse(path.exists())

    def test_release_refuses_an_unreadable_lease(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            path.write_text("{")
            proc = subprocess.run([sys.executable, str(CHECKER), "release", str(path),
                                   "--agent", AGENT, "--issue",
                                   "1552639d-ab20-49a4-8341-1c2c1b2468ef"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertEqual(EXIT_FOR_VERDICT[LEASE_UNREADABLE], proc.returncode)
            self.assertIn("LEASE_UNREADABLE", proc.stdout)
            self.assertTrue(path.exists(),
                            "release deleted a lease it could not read — fail closed means "
                            "it stays")


class TestTheBootWrapperVoice(unittest.TestCase):
    def test_verify_refuses_stale_and_unreadable_with_a_named_reason(self):
        import tempfile
        for text, needle in ((lease(acquiredAt=stamp(minutes=-200)), "STALE"),
                             ("{", "UNREADABLE"),
                             (lease(acquiredAt="yesterday"), "UNREADABLE"),
                             (lease(agentId=""), "UNREADABLE")):
            with self.subTest(needle=needle, text=text[:32]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "lease"
                    path.write_text(text)
                    proc = subprocess.run([sys.executable, str(CHECKER), "verify", str(path),
                                           "--label", "worldserver-up"],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          text=True, timeout=60)
                    self.assertEqual(2, proc.returncode)
                    self.assertIn("REFUSING BOOT", proc.stdout)
                    self.assertIn(needle, proc.stdout)
                    self.assertIn("worldserver-up:", proc.stdout)

    def test_verify_announces_a_live_lease_on_stdout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease"
            path.write_text(lease())
            proc = subprocess.run([sys.executable, str(CHECKER), "verify", str(path)],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60)
            self.assertEqual(0, proc.returncode)
            self.assertIn("lease held (agent ", proc.stdout)
            self.assertIn("min of the expected window left", proc.stdout)


class TestOneParserOnly(unittest.TestCase):
    """Drift guards. PRO-356 was two parsers disagreeing, so the doc gets to have none.

    These read the shipped files rather than a fixture, because the failure mode is exactly
    "someone re-adds the one-liner to the doc because it was convenient" — the doc is what
    agents copy-paste before they `rm -f` somebody's lease.
    """

    def test_the_doc_no_longer_carries_its_own_timestamp_parser(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertNotIn("fromdate", doc,
                         "the documented CHECK grew a jq timestamp parser again; that is how "
                         "PRO-356 happened")
        self.assertNotIn("fromisoformat", doc, "the doc carries its own parser again")
        self.assertIn("ops/deploy/lease_check.py check", doc,
                      "the CHECK in the doc must call the one reader")

    def test_the_doc_states_the_four_verdicts_and_the_fail_closed_rule(self):
        doc = DOC.read_text(encoding="utf-8")
        for token in (LEASE_FREE, LEASE_HELD_LIVE, LEASE_STALE_BREAKABLE, LEASE_UNREADABLE):
            self.assertIn(token, doc, f"{token} is not documented")
        self.assertNotIn("will not parse counts as stale-breakable", doc,
                         "the fail-open rule PRO-356 was filed against is back")

    def test_the_wrapper_shares_the_reader(self):
        wrapper = WRAPPER.read_text(encoding="utf-8")
        self.assertNotIn("fromisoformat(", wrapper,
                         "the boot wrapper grew its own timestamp parser again")
        self.assertNotIn("import datetime", wrapper,
                         "the boot wrapper grew its own timestamp parser again")
        self.assertNotIn("fromdate", wrapper, "the boot wrapper grew a jq predicate again")
        self.assertIn("lease_check.py", wrapper)

    def test_the_canonical_stamp_is_what_date_produces(self):
        self.assertEqual(CANONICAL_TIMESTAMP, "%Y-%m-%dT%H:%M:%SZ")
        self.assertRegex(now().strftime(CANONICAL_TIMESTAMP),
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestParseHelpers(unittest.TestCase):
    def test_parse_window_accepts_numeric_strings(self):
        self.assertEqual(180.0, parse_window("180"))
        self.assertEqual(90.0, parse_window(90))
        self.assertEqual(0.5, parse_window("0.5"))

    def test_parse_window_rejects_a_window_that_cannot_be_trusted(self):
        for raw in (None, True, "soon", 0, -1, float("nan"), float("inf"), [], {}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_window(raw)

    def test_the_pro356_stamp_parses_to_the_minute_it_was_taken(self):
        raw = "2026-11-07T19:23:14.086945Z"
        self.assertEqual(dt.datetime(2026, 11, 7, 19, 23, 14, 86945, tzinfo=UTC),
                         parse_acquired_at(raw))


if __name__ == "__main__":
    unittest.main(verbosity=2)
