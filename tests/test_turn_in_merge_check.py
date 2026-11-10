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
                ("waiting", HELD, "reviewer turn held: usage window (openai-codex) — resumes 11:31"),
                ("waiting", "HTTP 502", "reviewer turn waiting to retry (attempt 2 of "),
                # #2297 shadow: a conflict turn's wait is no held: line, and keeps its reason.
                ("waiting", "conflict turn waits: its changes-requested verdict is not answered",
                 "reviewer turn waiting to retry (attempt 2 of 4): conflict turn waits: its "),
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

    def test_a_queued_turn_a_maintainer_holds_says_who_and_how(self):
        # #2297 shadow F1: a held PR's turn stays pending (#477); "queued" would hide why.
        self.add("reviewer", "pending")
        hold = {"by": "maint", "how": "label", "at": "2026-11-10T10:00:00Z"}
        said = views.turn_note(self.db, "acme/widgets", 7, HEAD, "reviewer", lambda: hold)
        self.assertEqual(said, "reviewer turn held by maint via the diaktoros:hold label, "
                               "until released")
        self.assertEqual(views.turn_note(self.db, "acme/widgets", 7, HEAD, "reviewer",
                                         lambda: None), "reviewer turn queued")

        def broken():
            raise RuntimeError("unreadable")
        self.assertEqual(views.turn_note(self.db, "acme/widgets", 7, HEAD, "reviewer", broken),
                         "reviewer turn queued")

    def test_the_hold_is_read_only_for_a_queued_turn(self):
        self.add("reviewer", "running")
        views.turn_note(self.db, "acme/widgets", 7, HEAD, "reviewer",
                        lambda: self.fail("a running turn read the hold"))

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
        self.assertIn((ready.PENDING, f"not approved at head {HEAD[:7]} — reviewer turn held: "
                                      "usage window (openai-codex) — resumes 11:31"), kinds)
        state, summary = merge_check.decide(kinds)
        self.assertEqual(state, merge_check.PENDING)
        self.assertIn("resumes 11:31", summary)

    def test_a_change_request_names_the_fixers_turn(self):
        self.add("fixer", "running")
        self.assertEqual(self.kind_of("changes requested at head aaaaaaa (1 of 3 verdicts) — "
                                      "fixer turn running",
                                      reviews=[tmc.review("CHANGES_REQUESTED")]), ready.FAILURE)

    def test_a_maintainers_hold_names_itself_on_the_queued_review(self):
        from diaktoros import pr_hold
        self.add("reviewer", "pending")
        hold = {"by": "maint", "how": "comment", "at": "2026-11-10T10:00:00Z"}
        with mock.patch.object(pr_hold, "read", return_value=(hold, "")) as read:
            kinds = self.kinds(reviews=[])
        read.assert_called_with(self.loop, 7)
        self.assertIn((ready.PENDING, f"not approved at head {HEAD[:7]} — reviewer turn held by "
                                      "maint via /loop hold, until released"), kinds)

    def test_a_parked_escalation_names_the_adjudicators_turn(self):
        # #2297 shadow F2: the adjudicator's note on "parked awaiting adjudication".
        from diaktoros import explain, state as state_mod
        self.st.breach.parent.mkdir(parents=True, exist_ok=True)
        state_mod._atomic_write(self.st.breach, {"acme/widgets#7": {
            "pr": 7, "head": HEAD, "rounds": 3, "cap": 3, "reason": "r", "at": "now",
            "status": "awaiting-adjudication"}})
        self.loop["adjudicator"] = {"route": "w-breach"}
        self.add("adjudicator", "waiting", "HTTP 502", retries=1)
        with mock.patch.object(explain, "_ruled", return_value=None):
            kind = self.kind_of("escalated: parked awaiting adjudication — adjudicator turn "
                                "waiting to retry (attempt 2 of 4",
                                reviews=tmc.spent_cap() + [tmc.review("APPROVED")])
        self.assertEqual(kind, ready.PENDING)

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

    def test_an_issue_fix_a_triage_or_an_open_shadow_refreshes_nothing(self):
        # Their number is an issue's (#2297 shadow): there is no PR check to refresh.
        self.assertFalse(self.ended(self.add("issue_fixer", "launching"), "failed", "x"))
        self.assertFalse(self.ended(self.add("triage", "launching"), "failed", "x"))
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


class Description(unittest.TestCase):
    def test_a_long_reason_loses_its_middle_not_the_resume_time(self):
        # #2313: GitHub takes 140 characters; a hard cut dropped the note's tail.
        first = (f"not approved at head {HEAD[:7]} — review cap reached; a maintainer grants "
                 "another round — reviewer turn held: usage window (openai-codex) — resumes 11:31")
        said = merge_check.description(merge_check.PENDING, first)
        self.assertLessEqual(len(said), merge_check.STATUS_DESCRIPTION_MAX)
        self.assertTrue(said.startswith("Waiting: not approved at head aaaaaaa"), said)
        self.assertTrue(said.endswith("resumes 11:31"), said)
        self.assertIn("…", said)
        self.assertEqual(merge_check.description(merge_check.PENDING, "short"), "Waiting: short")


if __name__ == "__main__":
    unittest.main()
