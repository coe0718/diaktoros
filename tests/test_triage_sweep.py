"""#849: a webhook burst over the gateway's rate_limit drops triage deliveries (HTTP 429, no
redelivery). The watchdog sweep queues the triage of an issue the gate would have queued, and
doctor warns on a low rate_limit (tests in test_rate_limit_doctor.py). Driven through
``watchdog.sweep_loop`` (the enforcing entry point) and ``hermes dk status``."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, run_supervisor  # noqa: E402
from diaktoros import triage_sweep  # noqa: E402
from diaktoros.cli import operations  # noqa: E402
from scripts import watchdog  # noqa: E402

LABELS = ["bug", "feature", "P1"]


def iso(age_s: float) -> str:
    # Age is relative to the moment the issue is built, not module import: the sweep reads the
    # clock when it runs, and in a long suite run import time can be minutes (> GRACE_S) earlier.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s))


def issue(number, author="owner", age=3600.0, labels=(), pr=False):
    out = {"number": number, "state": "open", "user": {"login": author}, "created_at": iso(age),
           "labels": [{"name": name} for name in labels]}
    if pr:
        out["pull_request"] = {"url": "x"}
    return out


class Sweep(fg.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**config.load_id("one"),
                     "triage": {"route": "one-triage", "profile": "arbiter",
                                "authors": ["owner"], "labels": list(LABELS),
                                "max_labels": 3, "comment": False}}
        self.db = config.host_path("ledger")
        self.sup = run_supervisor.Supervisor(self.db)
        self.enqueued = []

    def fake_enqueue(self, loop, seat, number, head, *, turn_key=""):
        self.enqueued.append((seat, number, head, turn_key))
        with mock.patch.object(self.sup, "_spawn"):
            return self.sup.submit(f"{loop['repo']}:{number}:{head}:{seat}:{turn_key}",
                                   loop["repo"], number, head, seat, turn_key=turn_key)

    def sweep(self, issues, read=None):
        # ``read`` replaces the listing stub: pass ``gh.open_issues_read`` to run the real one.
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[]), \
             mock.patch.object(gh, "open_issues_read",
                               read or mock.Mock(return_value=(issues, ""))), \
             mock.patch.object(gate, "isolated_supervisor", return_value=self.sup), \
             mock.patch.object(gate, "enqueue_isolated", side_effect=self.fake_enqueue), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            return watchdog.sweep_loop(self.loop, self.st)

    def test_untriaged_issue_without_a_ledger_row_is_queued_by_the_sweep(self):
        lines = self.sweep([issue(7)])
        self.assertEqual(self.enqueued, [("triage", 7, config.TRIAGE_HEAD, "triage")])
        self.assertTrue(any("#7 triage queued late" in line and "webhook delivery was missed" in line
                            for line in lines), lines)
        self.assertEqual(list(self.st.late_triage()), [7])

    def test_issues_that_are_not_triages_to_queue_are_left_alone(self):
        self.fake_enqueue(self.loop, "triage", 6, config.TRIAGE_HEAD, turn_key="triage")
        self.enqueued.clear()
        lines = self.sweep([
            issue(1, labels=["bug"]),             # a person labelled it
            issue(2, author="stranger"),          # not in triage.authors
            issue(3, pr=True),                    # a pull request
            issue(4, age=30.0),                   # too new: the gate's delivery may still come
            issue(5, age=30 * 86400.0),           # a backlog from before the loop
            issue(6),                             # already has a triage row in the ledger
        ])
        self.assertEqual(self.enqueued, [])
        self.assertEqual([line for line in lines if "queued late" in line], [])
        self.assertEqual(self.st.late_triage(), {})

    def test_an_issue_a_maintainer_sent_to_the_fixer_is_left_alone(self):
        # Shadow review of #1005 (F2): the fix label is not a triage label, so triage_blocker
        # lets it through; only the sweep's own fix_label check keeps it out. Disabling that
        # check left every test green.
        self.loop = {**self.loop, "triage": {**self.loop["triage"], "fix_label": "agent-fix"}}
        self.sweep([issue(1, labels=["Agent-Fix"]),     # handed to the fixer (case-insensitive)
                    issue(2, labels=["unrelated"])])    # a label that is neither: still triaged
        self.assertEqual([e[1] for e in self.enqueued], [2])
        self.enqueued.clear()
        self.loop = {**self.loop, "triage": {k: v for k, v in self.loop["triage"].items()
                                             if k != "fix_label"}}
        self.sweep([issue(3, labels=["agent-fix"])])    # no fix label configured: triaged
        self.assertEqual([e[1] for e in self.enqueued], [3])

    def test_a_second_sweep_does_not_queue_it_again_and_the_sweep_is_bounded(self):
        many = [issue(n, age=7200.0 + n) for n in range(100, 100 + triage_sweep.PER_SWEEP + 4)]
        self.sweep(many)
        self.assertEqual(len(self.enqueued), triage_sweep.PER_SWEEP)
        self.assertEqual([e[1] for e in self.enqueued], sorted(e[1] for e in self.enqueued))
        self.enqueued.clear()
        self.sweep(many)                          # the rest, not the ones already queued
        self.assertEqual(len(self.enqueued), 4)
        self.enqueued.clear()
        self.sweep(many)
        self.assertEqual(self.enqueued, [])

    def test_a_saturated_listing_still_queues_the_newest_and_alerts(self):
        # #1037: past gh.MAX_ISSUE_PAGES the sweep queued nothing and said nothing. Driven
        # through watchdog.sweep_loop with the real gh.open_issues_read over stubbed pages.
        seen = []

        def fetch(loop, path, *args, **kwargs):
            seen.append(path)
            if "&page=" not in path:          # newest page: 99 PRs and the one missed issue
                return [issue(900 + n, pr=True) for n in range(99)] + [issue(7)], None
            return [issue(5000 + n, pr=True) for n in range(gh.REVIEW_PAGE_SIZE)], None

        real = gh.open_issues_read
        with mock.patch.object(gh, "fetch", side_effect=fetch):
            lines = self.sweep(None, read=real)
        self.assertIn("direction=desc", seen[0])
        self.assertEqual(len(seen), gh.MAX_ISSUE_PAGES)
        self.assertEqual([e[1] for e in self.enqueued], [7])
        self.assertTrue(any("older issues" in line and "not checked" in line
                            for line in lines), lines)

    def test_a_whole_listing_raises_no_incomplete_alert(self):
        lines = self.sweep([issue(7)])
        self.assertEqual([line for line in lines if "not checked" in line], [])

    def test_a_loop_without_triage_reads_no_issues(self):
        self.loop = config.load_id("one")
        with mock.patch.object(gh, "open_issues_read") as read:
            self.sweep([issue(7)])
        read.assert_not_called()
        self.assertEqual(self.enqueued, [])

    def test_status_names_an_issue_the_sweep_queued_late(self):
        self.sweep([issue(7)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
             mock.patch.object(operations, "_pinned_seat_notes", return_value=[]):
            rc = operations.cmd_status(mock.Mock(loop="one"))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("issue #7 triage queued late", out.getvalue())
        self.assertIn("the webhook delivery was missed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
