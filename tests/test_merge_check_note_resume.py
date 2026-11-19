"""The merge check's turn note keeps its resume time, and a changed hold refreshes the check.

#2325: a held line longer than ``views.TURN_NOTE_MAX`` lost its "— resumes …" end to the note's
bound before ``merge_check.description`` could keep the tail. #2326: a changed CI hold reason
written by the claim's light poll, and the claim's two terminal writes (a refused fixer row, an
unavailable review generation), never refreshed the merge check, so it showed the old reason until
the watchdog's sweep.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ci_poll as cp  # noqa: E402
import test_seat_models as sm  # noqa: E402
import test_turn_in_merge_check as tim  # noqa: E402
from diaktoros import config, merge_check, pacing  # noqa: E402
from diaktoros.run_supervisor import CI_HOLD, Supervisor, turns, views  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

UNTIL = 1794000000.0


class HeldNote(tim.Ledger, unittest.TestCase):
    def setUp(self):
        self.make_ledger()

    def held(self, reason):
        line = pacing.hold_line("reviewer", "openai-codex", UNTIL, reason)
        self.add("reviewer", "waiting", line)
        return self.note(), f"— resumes {pacing.when(UNTIL)}"

    def test_a_provider_unavailable_hold_keeps_its_resume_time(self):
        # The issue's own line: 99 characters, cut to "… — resumes 2026-1…" before #2325.
        note, resumes = self.held("provider unavailable (empty stream with no finish_reason)")
        self.assertEqual(note, "reviewer turn held: provider unavailable (empty stream with no "
                               f"finish_reason) {resumes}")

    def test_a_longer_reason_is_cut_before_the_resume_time(self):
        note, resumes = self.held("provider unavailable (" + "upstream said no; " * 8 + ")")
        self.assertTrue(note.endswith(resumes), note)
        self.assertIn("…", note)
        self.assertTrue(note.startswith("reviewer turn held: provider unavailable (upstream"))
        self.assertLessEqual(len(note), len("reviewer turn held: ") + views.TURN_NOTE_MAX)
        # and the commit status's one line keeps it too
        said = merge_check.description(merge_check.PENDING,
                                       f"not approved at head {tim.HEAD[:7]} — {note}")
        self.assertTrue(said.endswith(resumes), said)

    def test_a_long_held_line_without_a_resume_time_is_bounded_as_before(self):
        self.add("reviewer", "waiting", f"{CI_HOLD} on {tim.HEAD[:7]} — " + "check, " * 30)
        note = self.note()
        self.assertTrue(note.startswith("reviewer turn held: waiting for CI on aaaaaaa — "), note)
        self.assertLessEqual(len(note), len("reviewer turn held: ") + views.TURN_NOTE_MAX)
        self.assertTrue(note.endswith("…"), note)


class LightPollRefresh(sm.Base):
    # test_ci_poll's helpers, borrowed so its own tests do not run twice.
    fake_api, hold, claim, row = (cp.LightPoll.fake_api, cp.LightPoll.hold, cp.LightPoll.claim,
                                  cp.LightPoll.row)

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
            self.sup.enqueue("d-2326", "acme/widgets", 7, cp.HEAD, "reviewer")
        self.checks, self.pr, self.calls, self.kv = cp.RUNNING, cp.pull(), [], {}

    def polled(self, stale=None):
        """One light poll of the held row (its stored reason ``stale`` when given): whether the
        merge check was refreshed."""
        self.hold()
        if stale is not None:
            with ledger.connect(self.sup.db) as con:
                con.execute("UPDATE runs SET error=?", (stale,))
        with mock.patch.object(merge_check, "refresh_quietly") as refresh:
            self.assertIsNone(self.claim())
        self.assertEqual(self.row()[0], "waiting")
        self.assertTrue(self.row()[1].startswith(CI_HOLD))
        if refresh.called:
            self.assertEqual(refresh.call_args.args, (self.loop, 7))
        return refresh.called

    def test_a_changed_ci_reason_refreshes_the_check(self):
        self.assertTrue(self.polled(stale=f"{CI_HOLD} on {cp.HEAD[:7]} — lint still running"))

    def test_an_unchanged_ci_reason_costs_no_refresh(self):
        self.assertFalse(self.polled())     # hold() stores the reason the poll finds again


class ClaimTimeEnds(tim.Ledger, unittest.TestCase):
    """A row retired at its claim by a refusal or an unavailable read refreshes the check."""

    def setUp(self):
        self.make_ledger()
        self.loop = {"id": "w", "repo": "acme/widgets"}

    def committed(self, seat, **read):
        row = self.add(seat)
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(merge_check, "refresh_quietly") as refresh:
            self.assertIsNone(self.sup._claim_commit(row, turns._ClaimRead(**read)))
        return self.sup.get(row["delivery"])["state"], refresh

    def test_a_refused_fixer_row_refreshes_the_check(self):
        state, refresh = self.committed("fixer", refused="fixer not admitted")
        self.assertEqual(state, "cancelled")
        refresh.assert_called_once_with(self.loop, 7)

    def test_an_unavailable_review_generation_refreshes_the_check(self):
        state, refresh = self.committed("reviewer", unavailable="review generation unavailable")
        self.assertEqual(state, "failed")
        refresh.assert_called_once_with(self.loop, 7)


if __name__ == "__main__":
    unittest.main()
