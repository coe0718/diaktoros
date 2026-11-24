"""A sweep overlapping a push reports no reviewer stall for the head that just moved (#2626).

The sweep pairs a PR listing that can be seconds old with a fresh read of the run ledger. A
reviewer turn cancelled because the head moved before it started
(``run_supervisor.REVIEW_HEAD_MOVED``) is proof the listed head is gone: the next sweep lists the
new head, and #2468 delivers the review there. So that head is no "reviewer never posted a
verdict" stall. Any other ending (another cancellation, a failure, ``uncertain``) or no turn at
all still is, as before; so is a cancellation older than the sweep's listing read (not merely its
start), since the head can return to that SHA. The ledger here is real, so ``_stall_kind`` reads what the worker wrote.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, run_supervisor, watch_mode  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402
from scripts import watchdog  # noqa: E402

STALL = "reviewer never posted a verdict"
MOVED = run_supervisor.REVIEW_HEAD_MOVED


class HeadMovedIsNotAStall(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.db = config.host_path("ledger")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        run_supervisor.Supervisor(self.db)        # schema only: never spawns a worker
        self.n = 0

    def turn(self, state, error="", head=fg.HEAD, seat="reviewer", now=False):
        """A reviewer turn at the listed head, ended as the worker leaves it: before the sweep,
        or (``now``) at a point during it."""
        self.n += 1
        if now:
            time.sleep(0.002)           # strictly ordered after what the sweep stamped before
        when = time.time() if now else time.time() - 100 + self.n
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, attempts, "
                        "error, created, updated, turn_key, push_admitted, retries) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 0, 0)",
                        (f"r{self.n}", f"d{self.n}", fg.REPO, 7, head, seat, state, error,
                         when, when, f"k{self.n}"))

    def sweep(self, *after, inflight=(), before=()):
        """One sweep; turns ``(state, error)`` the worker ends once its listing came back
        (``after``), while that listing is being read (``inflight``), or once the sweep began but
        before the read (``before``, #2694 review F1)."""
        def ending(turns, step):
            def run(*args, **kw):
                for state, error in turns:
                    self.turn(state, error, now=True)
                return step(*args, **kw)
            return run
        with mock.patch.object(watch_mode, "TEST", True), \
                mock.patch.object(gate, "hooks_armed", return_value=True), \
                mock.patch.object(watchdog, "_hooks_armed", side_effect=ending(
                    before, watchdog._hooks_armed)), \
                mock.patch.object(watchdog, "_heal_and_redrive", side_effect=ending(
                    after, watchdog._heal_and_redrive)), \
                mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
                mock.patch.object(gh, "open_prs", side_effect=ending(inflight, lambda *_a, **_k: [fg.LIVE])), \
                mock.patch.object(gh, "reviews", return_value=[]), \
                mock.patch.object(watchdog.review_kick, "kick", return_value=False), \
                mock.patch.object(watchdog, "retry_pending_breaches"), \
                mock.patch.object(watchdog.routes, "fire"), \
                mock.patch.object(watchdog.observer, "notify"), \
                mock.patch.object(watchdog.observer, "retry", return_value=0), \
                mock.patch.object(watchdog.observer, "flush"):
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def arm_long_ago(self):
        self.sweep()
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 36000
        self.st.watch_save(watch)

    def test_a_head_move_hides_the_stall_only_from_a_listing_read_before_it(self):
        self.arm_long_ago()
        # The cancellation lands after this sweep's listing came back: that listing is stale.
        self.assertNotIn(STALL, self.sweep(("cancelled", MOVED)))
        # The next sweep's listing, read after it, still shows the head: it moved away and came
        # back. A failed review at the head in between redelivers nothing, so this is a stall.
        self.turn("failed", "reviewer crashed", head="c" * 40)
        self.assertIn(STALL, self.sweep())
        # #2694 review F1: cancelled once the sweep began, or while its listing was being read,
        # the head may have come back before GitHub listed it: nothing says it is stale.
        for when in ("before", "inflight"):
            with self.subTest(when):
                self.assertIn(STALL, self.sweep(**{when: [("cancelled", MOVED)]}))

    def test_no_turn_or_another_ending_still_stalls(self):
        self.arm_long_ago()
        self.assertIn(STALL, self.sweep())                       # no reviewer turn at all
        self.turn("cancelled", "PR closed before the review started")
        self.assertIn(STALL, self.sweep())
        self.turn("cancelled", MOVED, seat="fixer")              # another seat's cancellation
        self.assertIn(STALL, self.sweep())
        self.turn("cancelled", MOVED, head="c" * 40)             # a move at another head
        self.assertIn(STALL, self.sweep())

    def test_only_the_newest_turn_at_the_head_counts(self):
        self.arm_long_ago()
        self.assertIn(STALL, self.sweep(("cancelled", MOVED),
                                        ("uncertain", "the review's write may have happened")))

    def test_moved_on_reads_the_newest_turn_and_an_absent_ledger_is_false(self):
        since = time.time() - 200
        self.assertFalse(views.moved_on(self.db, fg.REPO, 7, fg.HEAD, "reviewer", since))
        self.turn("cancelled", MOVED)
        self.assertTrue(views.moved_on(self.db, fg.REPO, 7, fg.HEAD, "reviewer", since))
        self.assertFalse(views.moved_on(self.db, fg.REPO, 7, fg.HEAD, "reviewer", time.time()))
        self.assertFalse(views.moved_on(self.db, fg.REPO, 7, fg.HEAD, "fixer", since))
        self.assertFalse(views.moved_on(self.db.parent / "absent.sqlite", fg.REPO, 7, fg.HEAD,
                                        "reviewer", since))


if __name__ == "__main__":
    unittest.main()
