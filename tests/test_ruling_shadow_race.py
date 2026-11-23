"""Review on #2585 (round 4): a POST still under way, or one settled before its answer came, never
costs a finding both copies.

F1: a ruling's sweep found no filing of its title, then the shadow's sweep wrote its own (its POST
under way) before the ruling's claim: the claim's ``ValueError`` made the ruling's copy
``duplicate`` at once, and the shadow's POST, refused for good, left neither filed. Whether the
title is taken is now decided in the transaction that writes the row: taken only by a POST under
way, the claim goes back to ``recorded`` and waits. F8: a POST outlasting ``POSTING_STALE_S`` was
settled ``uncertain`` (the ruling's copy dropped on its strength), then GitHub refused it; nothing
could undo that, so the finding was lost. A late, definite refusal now settles the unknown: the
row is released, the refusal counted, and a copy dropped only on it taken up again.
Each drives the sqlite ledger and both real sweeps; only GitHub is mocked.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruling_shadow_dedup as dedup  # noqa: E402
import test_ruling_shadow_fence as fence  # noqa: E402
import test_ruling_shadow_settle as settle  # noqa: E402
from diaktoros import broker, shadow_issues  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

LATER = settle.LATER


class ClaimRace(fence.Base):
    """F1: the shadow's same-title filing lands between the ruling's check and its claim."""

    def shadow_mid_post(self):
        """The shadow's sweep, stopped inside its POST: its claim and row stand, under way."""
        self.shadow_post(mock.Mock(side_effect=settle.Crash))

    def shadow_post(self, send):
        try:
            return super().shadow_post(send)
        except settle.Crash:
            return []

    def race(self):
        """The ruling's sweep, with the shadow's sweep run between its check and its claim."""
        original, seen = Supervisor.record_shadow_filing, []

        def claim(sup, run_id, *args, **kwargs):
            if run_id == "a1" and not seen:
                seen.append(self.shadow_mid_post())
            return original(sup, run_id, *args, **kwargs)
        with mock.patch.object(Supervisor, "record_shadow_filing", claim):
            posts = self.ruling_sweep()
        self.assertEqual(seen, [None])                             # the race really ran
        return posts

    def setUp(self):
        super().setUp()
        fence.SameTitle.fallback(self)
        with ledger.connect(self.db) as con:      # filed beside the shadow, not ruled over it
            con.execute("UPDATE rulings SET shadow_run=NULL WHERE run_id='a1'")

    def late_answer(self, send):
        """The shadow's POST, still under way, answers with ``send``."""
        with self.sup._connect() as con:
            row = con.execute("SELECT * FROM shadow_issues WHERE run_id='s1'").fetchone()
            filing = con.execute("SELECT seq FROM filed_issues WHERE run_id='s1'").fetchone()[0]
        return shadow_issues._post(self.loop, self.sup, row, filing, 1, self.logged.append,
                                   send=send)

    def refuse(self, *_args):
        raise broker.BrokerNotSent("HTTP 403 refused")

    def test_a_claim_racing_a_live_post_waits_and_files_once_that_post_is_refused_for_good(self):
        self.assertEqual(self.race(), [])
        self.assertEqual((self.state("a1"), self.shadow_row()[0]), ("recorded", "posting"))
        self.last_try()
        self.assertEqual(self.late_answer(self.refuse), (None, False))
        self.assertEqual((self.shadow_row(), self.filed_row()),
                         (("denied", shadow_issues.MAX_ATTEMPTS), []))
        self.assertEqual([post["title"] for post in self.ruling_sweep()], [dedup.SHADOW_TITLE])
        self.assertEqual(self.state("a1"), "posted")

    def test_a_close_title_racing_a_live_post_waits_too(self):
        # The decision in the claim's transaction reads titles as the sweep's check does.
        close = dedup.SHADOW_TITLE.replace("new ", "")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET title=?, title_key=? WHERE run_id='a1'",
                        (close, close.casefold()))
        self.assertEqual(self.race(), [])                          # no second POST
        self.assertEqual(self.state("a1"), "recorded")

    def test_a_claim_racing_a_live_post_that_lands_is_a_duplicate(self):
        self.race()
        self.assertEqual(self.late_answer(lambda *_a: (301, "")), (301, True))
        self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual(self.state("a1"), "duplicate")


class LateRefusal(fence.Base):
    """F8: the shadow's POST outlasts ``POSTING_STALE_S``; the ruling's sweep settles it meanwhile."""

    def outlasted(self, answer, last=True):
        """The shadow's POST, during which the ruling's sweep settles it; then it answers."""
        self.run = self.ruled_over_shadow(settle.CITED, settle.CITED)
        seen = []

        def send(loop, **_kwargs):
            seen.append((self.ruling_sweep(later=LATER), self.shadow_row()[0],
                         self.state(self.run)))
            return answer()
        if last:
            self.last_try()
        posts = self.shadow_post(send)
        self.assertEqual(seen, [([], "uncertain", "duplicate")])   # settled meanwhile
        return posts

    def refused(self):
        raise broker.BrokerNotSent("HTTP 403 refused")

    def test_refused_for_good_after_being_settled_the_rulings_copy_is_filed(self):
        self.outlasted(self.refused)
        self.assertEqual((self.shadow_row(), self.filed_row()),
                         (("denied", shadow_issues.MAX_ATTEMPTS), []))
        self.assertEqual(self.state(self.run), "recorded")         # taken up again
        self.assertEqual(len(self.ruling_sweep()), 1)
        self.assertEqual(self.state(self.run), "posted")

    def test_refused_once_after_being_settled_the_shadows_copy_is_retried_and_filed_once(self):
        self.outlasted(self.refused, last=False)
        self.assertEqual((self.shadow_row(), self.filed_row()), (("recorded", 1), []))
        self.assertEqual(self.ruling_sweep(), [])                  # waits on the retry
        self.assertEqual(len(self.shadow_sweep()), 1)
        self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual((self.shadow_row()[0], self.state(self.run)), ("posted", "duplicate"))

    def test_filed_after_being_settled_it_stands_filed_and_nothing_is_replayed(self):
        self.outlasted(lambda: (301, ""))
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("posted", [("posted",)]))
        self.assertEqual((self.ruling_sweep(), self.shadow_sweep()), ([], []))
        self.assertEqual(self.state(self.run), "duplicate")

    def test_an_unknown_answer_after_being_settled_stays_unknown(self):
        def timeout():
            raise TimeoutError("timed out")
        self.outlasted(timeout)
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("uncertain", [("uncertain",)]))
        self.assertEqual((self.ruling_sweep(later=LATER), self.shadow_sweep(later=LATER)), ([], []))
        self.assertEqual(self.state(self.run), "duplicate")


if __name__ == "__main__":
    unittest.main()
