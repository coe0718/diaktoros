"""#629: a held run leaves the live states when its PR closes or its head moves, not only when
the run is drained. Driven through the watchdog sweep (the enforcing entry point), then read
back by ``stats.now`` (what the Desktop status bar shows)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, ledger, run_supervisor, stats  # noqa: E402
from diaktoros import watch_mode  # noqa: E402
from scripts import watchdog  # noqa: E402

HELD = "held: reviewer usage window (openai-codex), resumes later"
NEW_HEAD = "c" * 40


class StaleHeld(fg.Base):
    def setUp(self):
        super().setUp()
        self.loop = config.load_id("one")
        self.db = config.host_path("ledger")
        self.sup = run_supervisor.Supervisor(self.db)
        self.n = 0

    def held(self, pr, head, seat="reviewer"):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", fg.REPO, pr, head, seat)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', error=?, updated=? WHERE delivery=?",
                        (HELD, time.time(), f"d{self.n}"))

    def sweep(self, prs):
        with mock.patch.object(watch_mode, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=prs), \
             mock.patch.object(gh, "pr", side_effect=lambda *a, **k: prs[0] if prs else None), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            return watchdog.sweep_loop(self.loop, self.st)

    def live_runs(self):
        view = stats.now(self.db, fg.REPO)
        return {(r["pr"], r["head"]): r["state"] for r in view["runs"]}, view["counts"]

    def test_sweep_retires_held_runs_of_closed_prs_and_moved_heads(self):
        self.held(7, fg.HEAD)                  # PR 7 still at this head: stays held
        self.held(7, "d" * 40)                 # an older head of PR 7
        self.held(8, "e" * 40)                 # PR 8 is no longer open (merged)
        self.held(8, "e" * 40, seat="fixer")   # same PR, other seat (distinct turn)
        runs, counts = self.live_runs()
        self.assertEqual(counts["held"], 4)
        self.sweep([fg.LIVE])
        runs, counts = self.live_runs()
        self.assertEqual(runs, {(7, fg.HEAD[:7]): "waiting"})
        self.assertEqual(counts, {"held": 1})
        with ledger.connect(self.db) as con:
            errors = {r[0] for r in con.execute("SELECT error FROM runs WHERE state='cancelled'")}
        self.assertEqual(errors, {"PR closed before the review started",
                                  "PR head moved before the review started"})

    def test_an_unlisted_open_prs_runs_are_kept_when_the_listing_failed(self):
        self.held(8, "e" * 40)
        self.sweep(None)                       # no complete listing: nothing is known
        runs, _ = self.live_runs()
        self.assertEqual(runs, {(8, "eeeeeee"): "waiting"})


if __name__ == "__main__":
    unittest.main()
