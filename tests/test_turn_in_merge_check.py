"""#753: a loop turn that is queued, running, held or failed shows on the PR, through the merge
check's words (``views.turn_note``, appended by ``ready`` to the reason the turn answers), and the
worker refreshes the check when the turn launches and when it ends with something new to say.
The check's state never depends on the note."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_merge_check as tmc  # noqa: E402
from diaktoros import merge_check, ready, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor, views  # noqa: E402

HEAD = tmc.HEAD
HELD = "held: reviewer usage window (openai-codex) — resumes 11:31"


class Ledger:
    """A real run ledger with rows put in the states a test needs."""

    def make_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = pathlib.Path(tmp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True)
        self.sup._spawn = lambda: None

    def add(self, seat, state="pending", error=None, retries=0, retry_at=None, pr=7, head=HEAD,
            created=None):
        delivery = f"acme/widgets:{pr}:{head}:{seat}:{state}:{created}"
        self.sup.enqueue(delivery, "acme/widgets", pr, head, seat)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state=?, error=?, retries=?, retry_at=?, created=? "
                        "WHERE delivery=?", (state, error, retries, retry_at,
                                             created or time.time(), delivery))
        return self.sup.get(delivery)

    def note(self, seat="reviewer", pr=7, head=HEAD):
        return views.turn_note(self.db, "acme/widgets", pr, head, seat)


class Note(Ledger, unittest.TestCase):
    def setUp(self):
        self.make_ledger()

    def test_each_state_in_one_clause(self):
        for state, error, words in (
                ("pending", None, "reviewer turn queued"),
                ("claimed", None, "reviewer turn running"),
                ("running", None, "reviewer turn running"),
                ("waiting", HELD, f"reviewer turn {HELD}"),
                ("waiting", "HTTP 502", "reviewer turn waiting to retry (attempt 2 of "),
                ("failed", "budget killed", "reviewer turn failed: budget killed"),
                ("cancelled", "superseded", "reviewer turn cancelled: superseded"),
                ("uncertain", None, "reviewer turn outcome uncertain"),
                ("succeeded", None, "")):
            with self.subTest(state=state, error=error):
                self.make_ledger()
                self.add("reviewer", state, error, retries=1)
                self.assertEqual(self.note()[:len(words)] if words else self.note(), words)

    def test_the_newest_turn_at_this_head_and_seat_speaks(self):
        self.add("reviewer", "failed", "old", created=1000.0)
        self.add("reviewer", "pending", created=2000.0)
        self.add("fixer", "running")
        self.add("reviewer", "running", head="b" * 40)
        self.add("reviewer", "running", pr=8)
        self.assertEqual(self.note(), "reviewer turn queued")
        self.assertEqual(self.note("fixer"), "fixer turn running")
        self.assertEqual(self.note("adjudicator"), "")

    def test_a_long_or_multi_line_reason_is_one_bounded_line(self):
        self.add("reviewer", "failed", "line one\nline two " + "x" * 300)
        said = self.note()
        self.assertNotIn("\n", said)
        self.assertLessEqual(len(said), len("reviewer turn failed: ") + views.TURN_NOTE_MAX)

    def test_no_ledger_is_no_note(self):
        self.assertEqual(views.turn_note(self.db.parent / "none.sqlite", "acme/widgets", 7,
                                         HEAD, "reviewer"), "")


class Ready(Ledger, tmc.KindsBase):
    def setUp(self):
        super().setUp()
        self.make_ledger()
        patch = mock.patch.object(run_supervisor, "production_ledger", return_value=self.db)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_held_review_names_its_hold_and_the_check_still_waits(self):
        self.add("reviewer", "waiting", HELD)
        kinds = self.kinds(reviews=[])
        self.assertIn((ready.PENDING, f"not approved at head {HEAD[:7]} — reviewer turn {HELD}"),
                      kinds)
        state, summary = merge_check.decide(kinds)
        self.assertEqual(state, merge_check.PENDING)
        self.assertIn("resumes 11:31", summary)

    def test_a_change_request_names_the_fixers_turn(self):
        self.add("fixer", "running")
        self.assertEqual(self.kind_of("changes requested at head aaaaaaa (1 of 3 verdicts) — "
                                      "fixer turn running",
                                      reviews=[tmc.review("CHANGES_REQUESTED")]), ready.FAILURE)

    def test_the_note_never_changes_the_state(self):
        # An approved, green head with a failed turn left at it is still success.
        self.add("reviewer", "failed", "a stale failure")
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.SUCCESS)


class Refresh(Ledger, unittest.TestCase):
    """The worker refreshes the check when a PR turn launches and when it ends with news."""

    def setUp(self):
        self.make_ledger()
        self.loop = {"id": "w", "repo": "acme/widgets"}
        patch = mock.patch.object(merge_check, "refresh_quietly")
        self.refresh = patch.start()
        self.addCleanup(patch.stop)

    def ended(self, row, state, error, launched=False):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state=?, error=? WHERE id=?", (state, error, row["id"]))
        self.refresh.reset_mock()
        self.sup._refresh_check(self.loop, row, state, launched=launched)
        return self.refresh.called

    def test_a_new_hold_a_failure_or_a_launched_turn_refreshes(self):
        row = self.add("reviewer", "launching")
        self.assertTrue(self.ended(row, "waiting", HELD))
        self.assertTrue(self.ended(row, "failed", "boom"))
        self.assertTrue(self.ended(row, "cancelled", "superseded"))
        self.assertTrue(self.ended(row, "succeeded", None, launched=True))
        self.assertEqual(self.refresh.call_args.args, (self.loop, 7))

    def test_a_ci_hold_repolled_unchanged_costs_no_refresh(self):
        row = self.add("reviewer", "launching", error="held: waiting for CI on aaaaaaa — tests")
        self.assertFalse(self.ended(row, "waiting", "held: waiting for CI on aaaaaaa — tests"))
        self.assertTrue(self.ended(row, "waiting", "held: waiting for CI on aaaaaaa — lint"))

    def test_an_issue_fix_or_an_open_shadow_refreshes_nothing(self):
        self.assertFalse(self.ended(self.add("issue_fixer", "launching"), "failed", "x"))
        self.assertFalse(self.ended(self.add(run_supervisor.SHADOW, "launching"), "waiting", "x"))
        self.assertTrue(self.ended(self.add(run_supervisor.SHADOW, "launching", pr=8),
                                   "failed", "x"))

    def test_the_launch_records_then_shows_the_turn_running(self):
        from diaktoros.run_supervisor import turns
        row = self.add("reviewer", "launching")
        turn = turns._Turn(run_id=row["id"], owner="w", budget=60, row=row, loop=self.loop)
        order = []
        with mock.patch.object(Supervisor, "record_launch",
                               side_effect=lambda *a: order.append("record")):
            self.refresh.side_effect = lambda *a: order.append("refresh")
            self.sup._launched(turn, "rev", "model-x")
        self.assertEqual(order, ["record", "refresh"])
        self.assertTrue(turn.launched)


if __name__ == "__main__":
    unittest.main()
