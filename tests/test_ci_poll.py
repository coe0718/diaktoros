"""#1648: a reviewer row held for CI is polled lightly, and the full claim runs once.

A held row is re-polled every ``CI_POLL_S`` for up to an hour. Each poll used to repeat the whole
claim read set (the PR, the reviews, the maintainer hold's two reads) and the hold's own reads;
now it reads the PR once (has it moved?) and the head's check-runs and statuses, and the full
claim runs once, when CI settles. These tests drive the worker's ``_claim``, the entry point.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import time
import unittest
from unittest import mock

import test_seat_models as sm
from diaktoros import ci, config, gh, ledger, observer, run_supervisor
from diaktoros.run_supervisor import CI_HOLD, Supervisor

HEAD = sm.HEAD
RUNNING = {"check_runs": [{"id": 1, "name": "tests", "status": "in_progress", "conclusion": None}],
           "total_count": 1}
GREEN = {"check_runs": [{"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}],
         "total_count": 1}


def pull(sha=HEAD, **extra) -> dict:
    return {"number": 7, "state": "open", "draft": False, "mergeable_state": "clean",
            "head": {"sha": sha, "ref": "fix-7", "repo": {"full_name": "acme/widgets"}},
            "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "acme/widgets"}},
            "user": {"login": "fix"}, **extra}


class LightPoll(sm.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "review_after_ci": True, "base": "main",
                     "triage": {"maintainers": ["boss"]}}
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps(self.settings))
        runtime.chmod(0o600)
        self.sup = Supervisor(self.root / "ledger.sqlite", production_config=runtime,
                              hermes_home=self.home)
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue("d-1648", "acme/widgets", 7, HEAD, "reviewer")
        self.checks = RUNNING
        self.pr = pull()
        self.calls: list[str] = []
        self.kv: dict = {}

    def fake_api(self, _loop, path, *a, **kw):
        self.calls.append(path.split("?")[0].replace("/repos/acme/widgets", ""))
        if "/check-runs" in path:
            return self.checks
        if path.endswith("/status") or "/status?" in path:
            return {"state": "pending", "statuses": [], "total_count": 0}
        return self.pr

    def hold(self):
        """What the worker's recover() leaves: the held row, due again, pending."""
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='pending', retry_at=NULL, owner=NULL, "
                        "error=?", (f"{CI_HOLD} on {HEAD[:7]} — 1 check(s) still running or not reported",))

    def claim(self, marker=None, holds=None, events=(), breach_error=None, events_error=""):
        self.calls.clear()
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(gh, "api", side_effect=self.fake_api), \
             mock.patch.object(gh, "reviews", side_effect=lambda *a, **k: self.calls.append("reviews") or []), \
             mock.patch.object(gh, "issue_events_read",
                               side_effect=lambda *a, **k: self.calls.append("events")
                               or ((None, events_error) if events_error else (list(events), ""))), \
             mock.patch.object(gh, "issue_comments_read",
                               side_effect=lambda *a, **k: self.calls.append("comments") or ([], "")), \
             mock.patch("diaktoros.state.state_for") as state_for, \
             mock.patch.object(observer, "notify") as notify:
            self.notify = notify
            st = state_for.return_value
            st.breach_get.return_value = marker
            if breach_error is not None:
                st.breach_get.side_effect = breach_error
            st.pr_holds.return_value = holds or {}
            st.kv_get.side_effect = lambda path, default: self.kv.get(path.name, default)
            st.kv_set.side_effect = lambda path, data: self.kv.__setitem__(path.name, data)
            st.dir = self.root
            return self.sup._claim()

    def age(self, seconds):
        """Queue the row ``seconds`` ago."""
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET created=?", (time.time() - seconds,))

    def row(self):
        with ledger.connect(self.sup.db) as con:
            return con.execute("SELECT state, error, retry_at FROM runs").fetchone()

    def test_a_held_row_makes_two_or_three_calls_per_poll(self):
        self.hold()
        for poll in range(5):
            with self.subTest(poll=poll):
                before = time.time()
                self.assertIsNone(self.claim())
                self.assertLessEqual(len(self.calls), 3, self.calls)
                self.assertNotIn("reviews", self.calls)
                self.assertNotIn("events", self.calls)
                self.assertEqual(self.calls.count("/pulls/7"), 1)
                state, error, retry_at = self.row()
                self.assertEqual(state, "waiting")
                self.assertTrue(error.startswith(CI_HOLD))
                self.assertGreaterEqual(retry_at, before + run_supervisor.CI_POLL_S)
                self.assertTrue(self.sup.held_for_ci)    # its worker lingers to re-queue it
                self.hold()

    def test_when_ci_turns_green_the_full_claim_runs_once(self):
        self.hold()
        self.assertIsNone(self.claim())
        self.assertNotIn("reviews", self.calls)
        self.hold()
        self.checks = GREEN
        claimed = self.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(self.calls.count("reviews"), 1, self.calls)   # the full claim's reads
        self.assertEqual(self.calls.count("events"), 1, self.calls)
        self.assertEqual(self.row()[0], "claimed")

    def test_a_moved_head_still_ends_the_hold(self):
        self.hold()
        # CI runs for three polls: the light poll alone keeps the hold (no review or hold
        # reads), which the pre-#1648 claim did not do, so this fails without the change.
        for _ in range(3):
            self.assertIsNone(self.claim())
            self.assertLessEqual(len(self.calls), 3, self.calls)
            self.assertNotIn("reviews", self.calls)
            self.assertNotIn("events", self.calls)
            self.assertTrue(self.row()[1].startswith(CI_HOLD))
            self.hold()
        # then the head moves: one PR read ends the hold, with no CI read and no review read
        self.pr = pull(sha="c" * 40)
        self.assertIsNone(self.claim())
        state, error, _ = self.row()
        self.assertEqual((state, error), ("cancelled", "PR head moved before the review started"))
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)   # no CI read first
        self.assertNotIn("reviews", self.calls)
        self.assertEqual(self.calls.count("/pulls/7"), 2, self.calls)   # the poll's, then the claim's

    def test_a_light_poll_sends_the_held_notice_once_per_kind(self):
        """A running hold is silent. The poll that finds a cancelled check sends the same
        ``held`` notice a held turn would, launching nothing; every later poll presents the
        same identity, so the observer records it once."""
        self.hold()
        self.assertIsNone(self.claim())
        self.notify.assert_not_called()                     # the normal wait for CI is silent
        self.checks = {"check_runs": [{"id": 1, "name": "tests", "status": "completed",
                                       "conclusion": "cancelled"}], "total_count": 1}
        identities = []
        for _ in range(2):
            self.hold()
            self.assertIsNone(self.claim())
            self.assertNotIn("reviews", self.calls)
            self.assertIn("cancelled", self.row()[1])
            self.assertEqual(self.notify.call_count, 1, self.notify.call_args_list)
            args, kw = self.notify.call_args
            self.assertEqual(args[2], "held")
            self.assertIn("cancelled", kw["outcome"])
            identities.append(kw["identity"])
            self.notify.reset_mock()
        self.assertEqual(len(set(identities)), 1, identities)
        self.assertTrue(identities[0].endswith(":held:ci-cancelled"), identities)

    def test_a_closed_or_unreadable_pr_goes_to_the_full_claim(self):
        for pr, state in ((pull(state="closed"), "cancelled"), (None, "waiting")):
            with self.subTest(pr=pr):
                self.hold()
                self.pr = pr
                self.assertIsNone(self.claim())
                self.assertEqual(self.row()[0], state)
                self.assertFalse(self.row()[1].startswith(CI_HOLD))
                with ledger.connect(self.sup.db) as con:
                    con.execute("UPDATE runs SET state='pending', retries=0")

    def test_a_row_not_held_for_ci_takes_the_full_claim(self):
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET error=NULL")
        self.assertIsNotNone(self.claim())
        self.assertEqual(self.calls.count("reviews"), 1, self.calls)

    def test_an_escalated_head_is_retired_while_ci_holds(self):
        """#1708: an operator's escalation (#459) at this head retires the held row at the next
        poll, as the full claim would, even under a red hold that has no time limit."""
        self.hold()
        self.assertIsNone(self.claim())
        self.assertEqual(self.row()[0], "waiting")             # control: still held for CI
        self.hold()
        marker = {"head": HEAD, "status": "awaiting-adjudication", "escalated": {"by": "boss"}}
        self.assertIsNone(self.claim(marker=marker))
        self.assertEqual(self.row()[:2], ("cancelled", "escalated to adjudication at this head"))
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)   # no CI read spent

    def test_an_escalation_at_an_older_head_keeps_the_light_poll(self):
        self.hold()
        marker = {"head": "c" * 40, "status": "awaiting-adjudication", "escalated": {"by": "boss"}}
        self.assertIsNone(self.claim(marker=marker))
        self.assertEqual(self.row()[0], "waiting")
        self.assertNotIn("events", self.calls)

    def test_a_maintainer_held_pr_takes_the_full_claims_hold_read(self):
        """#1708: a PR the sweep recorded as held (#477) leaves the CI hold for the claim's live
        hold read, which parks it pending like any held PR's row, not re-timed as a CI hold."""
        hold = {"by": "boss", "at": "2026-01-01T00:00:00Z", "how": "label"}
        labeled = [{"event": "labeled", "label": {"name": "diaktoros:hold"},
                    "actor": {"login": "boss"}, "created_at": "2026-01-01T00:00:00Z"}]
        self.hold()
        self.assertIsNone(self.claim(holds={7: hold}, events=labeled))
        self.assertIn("events", self.calls)                    # the claim's pr_hold.read ran
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)
        state, _, retry_at = self.row()
        self.assertEqual((state, retry_at), ("pending", None))  # parked by the hold, not re-timed
        self.assertFalse(self.sup.held_for_ci)

    def test_an_unrecorded_hold_is_read_live_once_per_bound(self):
        """#1708 (review F1): the sweep never recorded the hold (no sweep yet, a failed read, a
        first sweep that stops before the hold scan). Past ``CI_HOLD_READ_S`` of the hold the
        light poll reads it live, and the claim parks the row pending; before that bound, and
        within one period of the last live read, it does not spend the two calls."""
        labeled = [{"event": "labeled", "label": {"name": "diaktoros:hold"},
                    "actor": {"login": "boss"}, "created_at": "2026-01-01T00:00:00Z"}]
        self.hold()
        self.assertIsNone(self.claim(events=labeled))           # young hold: light poll only
        self.assertNotIn("events", self.calls)
        self.assertEqual(self.row()[0], "waiting")
        self.hold()
        self.age(run_supervisor.CI_HOLD_READ_S + 5)
        self.sup.held_for_ci = False                            # set by the first poll
        self.assertIsNone(self.claim(events=labeled))           # due: the live read sees it
        self.assertIn("events", self.calls)
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)
        state, _, retry_at = self.row()
        self.assertEqual((state, retry_at), ("pending", None))  # parked by the hold
        self.assertFalse(self.sup.held_for_ci)

    def test_a_live_hold_read_that_finds_nothing_waits_a_period(self):
        self.hold()
        self.age(run_supervisor.CI_HOLD_READ_S + 5)
        self.assertIsNone(self.claim())                         # due: read live, not held
        self.assertEqual(self.calls.count("events"), 1, self.calls)
        self.assertEqual(self.row()[0], "waiting")
        self.assertTrue(self.row()[1].startswith(CI_HOLD))
        self.hold()
        self.assertIsNone(self.claim())                         # the next poll: light again
        self.assertNotIn("events", self.calls)
        self.assertLessEqual(len(self.calls), 3, self.calls)

    def test_an_unreadable_escalation_takes_the_full_claim(self):
        """#1708 (review F2): a failed escalation read is not "not escalated": the row leaves
        the CI hold for the full claim, which backs it off as a failed claim read."""
        self.hold()
        self.assertIsNone(self.claim(breach_error=OSError("breach file unreadable")))
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)   # no CI read spent
        state, error, _ = self.row()
        self.assertEqual(state, "waiting")
        self.assertFalse(error.startswith(CI_HOLD), error)
        self.assertTrue(error.startswith(run_supervisor.CLAIM_READ_FAILED), error)
        self.assertIn("breach file unreadable", error)

    def test_an_unreadable_live_hold_takes_the_full_claim(self):
        """#1708 (review F3): a due live hold read that fails is not "not held": the row leaves
        the CI hold for the full claim, whose own hold read backs it off as a failed claim
        read, rather than being re-timed as a CI hold."""
        self.hold()
        self.age(run_supervisor.CI_HOLD_READ_S + 5)
        self.assertIsNone(self.claim(events_error="HTTP 502 on issue events"))
        self.assertNotIn("/commits/" + HEAD + "/check-runs", self.calls)   # no CI read spent
        self.assertEqual(self.calls.count("events"), 2, self.calls)       # the poll's, the claim's
        state, error, _ = self.row()
        self.assertEqual(state, "waiting")
        self.assertFalse(error.startswith(CI_HOLD), error)
        self.assertTrue(error.startswith(run_supervisor.CLAIM_READ_FAILED), error)
        self.assertIn("HTTP 502 on issue events", error)


if __name__ == "__main__":
    unittest.main()
