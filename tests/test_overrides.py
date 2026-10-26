"""Learn from overrides (#220), first step: a PR merged over a standing changes-requested at its
merged head writes one record, and the weekly report lists it; a merge after an approval writes
none.

GitHub is faked (``gh.api`` for the closed-PR listing, ``gh.reviews_read``); the run ledger, the
loop state and the watchdog sweep are real.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import gate, gh, ledger, overrides, run_supervisor, stats  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from scripts import watchdog  # noqa: E402

HEAD, EARLIER = fg.HEAD, "e" * 40


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(rid, state, head=HEAD, at="2026-01-01T00:00:00Z", login="reviewer"):
    return {**fg.verdict(rid, state), "commit_id": head, "submitted_at": at,
            "user": {"login": login}}


class Override(fg.Base):
    def setUp(self):
        super().setUp()
        self.db = run_supervisor.production_ledger()
        self.sup = Supervisor(self.db)
        self.merged_at = time.time() - 3600
        self.closed = [self.merged_pr()]
        self.reviews = {7: [review(5, "CHANGES_REQUESTED")]}

    def merged_pr(self, number=7, head=HEAD, merged=True, author="fixer"):
        return {**fg.LIVE, "number": number, "state": "closed", "user": {"login": author},
                "head": {"sha": head, "ref": f"fix-{number}"},
                "merged_at": iso(self.merged_at) if merged else None}

    def api(self, loop, path, *a, **k):
        return self.closed if "state=closed" in path else None

    def read(self, loop, number, login=None):
        got = self.reviews.get(number)
        return (got, "") if got is not None else (None, "HTTP 502")

    def rows(self):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT pr, head, kind, seat, model, review_id, findings "
                               "FROM overrides").fetchall()

    def reviewed(self, model, review_id=5):
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue("rv", fg.REPO, 7, HEAD, "reviewer")
        with ledger.connect(self.db) as con:
            run_id = con.execute("SELECT id FROM runs WHERE delivery='rv'").fetchone()[0]
            con.execute("UPDATE runs SET model=? WHERE id=?", (model, run_id))
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                        "review_id,verdict,created) VALUES(?,?,?,?,?,?,?)",
                        (run_id, "confirmed", "{}", 1, review_id, "REQUEST_CHANGES", 0))

    def scan(self):
        with mock.patch.object(gh, "api", side_effect=self.api), \
             mock.patch.object(gh, "reviews_read", side_effect=self.read):
            return overrides.scan(self.loop, self.st, self.sup)

    def sweep(self):
        """The watchdog sweep itself, with the open-PR listing empty and GitHub faked."""
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[]), \
             mock.patch.object(gh, "api", side_effect=self.api), \
             mock.patch.object(gh, "reviews_read", side_effect=self.read), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            return watchdog.sweep_loop(self.loop, self.st)


class Recording(Override):
    def test_a_merge_over_a_current_changes_requested_writes_one_record(self):
        self.reviewed("model-a")
        self.st.findings_put(7, {"findings": {"F1": {"state": "open"}, "F2": {"state": "fixed"},
                                              "F3": {"state": "open"}}})
        self.assertEqual(self.scan(), [7])
        self.assertEqual(self.rows(), [(7, HEAD, "merged_over_changes", "reviewer", "model-a", 5,
                                        json.dumps(["F1", "F3"]))])
        self.assertEqual(self.scan(), [])                    # a rescan: still one
        self.assertEqual(len(self.rows()), 1)

    def test_a_merge_after_an_approval_writes_none(self):
        self.reviews = {7: [review(5, "CHANGES_REQUESTED", at="2026-01-01T00:00:00Z"),
                            review(6, "APPROVED", at="2026-01-02T00:00:00Z")]}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.rows(), [])

    def test_only_a_verdict_at_the_merged_head_counts(self):
        self.reviews = {7: [review(5, "CHANGES_REQUESTED", head=EARLIER)]}   # fixed since
        self.assertEqual(self.scan(), [])
        self.reviews = {7: [review(5, "CHANGES_REQUESTED", login="stranger")]}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.rows(), [])

    def test_a_dismissed_changes_requested_is_not_standing(self):
        self.reviews = {7: [review(5, "DISMISSED")]}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.rows(), [])

    def test_unmerged_old_and_foreign_prs_write_none(self):
        stale = self.merged_pr(8)
        stale["merged_at"] = iso(time.time() - 30 * 86400)
        self.closed = [self.merged_pr(7, merged=False), stale,
                       self.merged_pr(9, author="stranger")]
        self.reviews = {n: [review(5, "CHANGES_REQUESTED")] for n in (7, 8, 9)}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.rows(), [])

    def test_unreadable_reviews_are_tried_again_not_guessed(self):
        self.reviews = {}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.rows(), [])
        self.reviews = {7: [review(5, "CHANGES_REQUESTED")]}
        self.assertEqual(self.scan(), [7])

    def test_unorderable_verdicts_are_read_again_not_marked_checked(self):
        undated = {**review(5, "CHANGES_REQUESTED"), "submitted_at": None}
        self.reviews = {7: [undated]}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.st.override_checked(), {})        # not marked as read
        self.reviews = {7: [review(5, "CHANGES_REQUESTED")]}     # now provable
        self.assertEqual(self.scan(), [7])
        self.assertEqual(self.st.override_checked(), {"7": HEAD})

    def test_a_merge_with_no_verdict_at_the_head_is_marked_checked(self):
        self.reviews = {7: [review(5, "COMMENTED"), review(6, "CHANGES_REQUESTED", head=EARLIER)]}
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.st.override_checked(), {"7": HEAD})

    def test_a_merge_already_read_is_not_read_again(self):
        self.reviews = {7: [review(5, "APPROVED")]}
        self.scan()
        with mock.patch.object(gh, "api", side_effect=self.api), \
             mock.patch.object(gh, "reviews_read") as read:
            overrides.scan(self.loop, self.st, self.sup)
        read.assert_not_called()


class Sweep(Override):
    """The enforcing entry point: the watchdog sweep records it; removing its call turns this red."""

    def test_the_sweep_records_a_merge_over_changes_requested_once(self):
        self.sweep()
        self.sweep()
        self.assertEqual([(r[0], r[1], r[2]) for r in self.rows()],
                         [(7, HEAD, "merged_over_changes")])

    def test_the_sweep_records_nothing_for_a_merge_after_an_approval(self):
        self.reviews = {7: [review(5, "APPROVED")]}
        self.sweep()
        self.assertEqual(self.rows(), [])

    def test_a_scan_that_fails_never_fails_the_sweep(self):
        with mock.patch.object(overrides, "scan", side_effect=RuntimeError("boom")):
            self.assertIsInstance(self.sweep(), list)


class Report(Override):
    def test_the_report_lists_the_override_with_seat_model_and_findings(self):
        self.reviewed("model-a")
        self.st.findings_put(7, {"findings": {"F1": {"state": "open"}}})
        self.scan()
        data = stats.report(self.loop, self.db, self.merged_at - 86400, self.merged_at + 86400,
                            breaches={}, pr_state=lambda n: None, tracked={})
        got = data["overrides"]
        self.assertEqual((got["merged_over_changes"], got["models"], got["findings"]),
                         (1, {"model-a": 1}, 1))
        self.assertEqual(got["cases"], [{"pr": 7, "head": HEAD[:7], "seat": "reviewer",
                                         "model": "model-a", "findings": ["F1"]}])
        text = stats.report_text(data)
        self.assertIn("human overrides: 1 merged over a changes-requested · model-a 1 "
                      "· 1 open finding(s) overridden", text)
        self.assertIn(f"  #7 merged at {HEAD[:7]} over reviewer (model-a): F1", text)
        self.assertEqual(json.loads(json.dumps(data))["overrides"]["merged_over_changes"], 1)
        later = stats.overrides(self.db, fg.REPO, self.merged_at + 86400, self.merged_at + 2e5)
        self.assertEqual(later["merged_over_changes"], 0)     # outside the window

    def test_a_ledger_before_the_table_reports_none_not_an_error(self):
        with ledger.connect(self.db) as con:
            con.execute("DROP TABLE overrides")
        self.assertEqual(stats.overrides(self.db, fg.REPO, 0, 4e9)["merged_over_changes"], 0)


if __name__ == "__main__":
    unittest.main()
