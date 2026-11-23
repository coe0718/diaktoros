"""Review on #2585 (F8, again): a late, definite refusal landing between a sweep's read and its
drop. A copy was dropped as a ``MAYBE`` duplicate on a read made before the refusal, which had
already revived what it could, so the finding was lost. The drop is now decided again in the
transaction that writes it (``drop_copy``), and a claim that finds its title held only by an
unknown settles in the transaction that finds it, so a refusal lands wholly before or after.
Each interleaving drives the sqlite ledger and the real sweeps; only GitHub is mocked.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruling_shadow_dedup as dedup  # noqa: E402
import test_ruling_shadow_fence as fence  # noqa: E402
import test_ruling_shadow_settle as settle  # noqa: E402
from diaktoros import broker, ruling_issues, shadow_issues  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

LATER, MAX = settle.LATER, shadow_issues.MAX_ATTEMPTS


def refuse(*_args):
    raise broker.BrokerNotSent("HTTP 403 refused")


class Interleave(fence.Base):
    def late_refusal(self, run_id):
        """``run_id``'s POST, settled ``uncertain`` meanwhile, is refused for good now."""
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET attempts=? WHERE run_id=?", (MAX - 1, run_id))
        with self.sup._connect() as con:
            row = con.execute("SELECT * FROM shadow_issues WHERE run_id=?", (run_id,)).fetchone()
            filing = con.execute("SELECT seq FROM filed_issues WHERE run_id=?",
                                 (run_id,)).fetchone()[0]
        self.assertEqual(shadow_issues._post(self.loop, self.sup, row, filing, 1,
                                             self.logged.append, send=refuse), (None, False))

    def refused_after(self, module, name, run_id):
        """Patch ``module.name``: its first read outside a transaction is followed, before the
        sweep acts on it, by ``run_id``'s late refusal. What that read found."""
        original, seen = getattr(module, name), []

        def read(*args, con=None):
            found = original(*args, con=con) if con is not None else original(*args)
            if con is None and not seen:
                seen.append(found)
                self.late_refusal(run_id)
            return found
        return mock.patch.object(module, name, read), seen



class Refusals(Interleave):
    """The adjudicator's ``a1`` and the shadow's ``s1`` ask for the same title, side by side."""

    def shadow_mid_post(self):
        """The shadow's sweep, stopped inside its POST: its claim and row stand, under way."""
        with contextlib.suppress(settle.Crash):
            self.shadow_post(mock.Mock(side_effect=settle.Crash))

    def setUp(self):
        super().setUp()
        fence.SameTitle.fallback(self)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE rulings SET shadow_run=NULL WHERE run_id='a1'")

    def test_the_shadows_copy_read_as_maybe_then_refused_is_filed_not_dropped(self):
        with mock.patch.object(ruling_issues, "_send", side_effect=settle.Crash):
            with self.assertRaises(settle.Crash):
                self.ruling_sweep()                     # a1's POST under way, its sweep stopped
        patch, seen = self.refused_after(shadow_issues, "_filed", "a1")
        with patch:
            self.assertEqual(self.shadow_sweep(later=LATER), [])
        self.assertEqual(seen, ["maybe"])               # a1 settled uncertain, then refused
        self.assertEqual((self.state("a1"), self.shadow_row()[0]), ("denied", "recorded"))
        self.assertEqual([post["title"] for post in self.shadow_sweep()], [dedup.SHADOW_TITLE])

    def test_a_claim_finding_only_an_unknown_then_refused_is_taken_up_again(self):
        original, seen = Supervisor.record_shadow_filing, []

        def claim(sup, run_id, *args, **kwargs):
            if run_id != "a1" or seen:
                return original(sup, run_id, *args, **kwargs)
            seen.append(self.shadow_mid_post())         # s1's POST under way, then settled
            with ledger.connect(self.db) as con:        # s1's claim stale, a1's own still fresh
                con.execute("UPDATE shadow_issues SET updated=updated-? WHERE run_id='s1'",
                            (LATER,))
            shadow_issues.settle_stale(self.sup, dedup.REPO, time.time(), self.logged.append)
            try:
                return original(sup, run_id, *args, **kwargs)
            except ValueError as taken:
                seen.append(type(taken).__name__)
                raise
            finally:
                self.late_refusal("s1")
        with mock.patch.object(Supervisor, "record_shadow_filing", claim):
            self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual(seen, [None, "MaybeFiled"])    # held only by s1's unknown POST
        self.assertEqual((self.shadow_row(), self.state("a1")), (("denied", MAX), "recorded"))
        self.assertEqual([post["title"] for post in self.ruling_sweep()], [dedup.SHADOW_TITLE])


class RulingRead(Interleave):
    """The ruling over ``s1`` reads its request as ``maybe``; then the refusal lands."""

    def test_the_rulings_copy_read_as_maybe_then_refused_is_filed_not_dropped(self):
        run = self.ruled_over_shadow(settle.CITED, settle.CITED)
        self.shadow_sweep(crash="post")                 # s1's POST under way, its sweep stopped
        patch, seen = self.refused_after(ruling_issues, "_shadow_asked", "s1")
        with patch:
            self.assertEqual(self.ruling_sweep(later=LATER), [])
        self.assertEqual(seen, ["maybe"])
        self.assertEqual((self.shadow_row(), self.state(run)), (("denied", MAX), "recorded"))
        self.assertEqual(len(self.ruling_sweep()), 1)
        self.assertEqual(self.state(run), "posted")


if __name__ == "__main__":
    unittest.main()
