"""Shadow filings across runs and in the sweep's log.

#1978: ``Supervisor.filed_titles`` covers every issue filed (or being filed) from a PR, whoever
filed it, so two shadow runs on one PR never both file one finding. #1984: a refusal that is
retried or dropped names the finding's title in the sweep's log, as the uncertain and expiry
messages do, so findings refused alike on one PR are told apart.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shadow_issues as base  # noqa: E402
from diaktoros import broker, gh, shadow_issues  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

REPO = base.REPO
FIRST, SECOND = "Docs say the flag is --x", "Docs say the flag is --x."


class Fixture(unittest.TestCase):
    # The FilePending fixture's plumbing, borrowed (inheriting would rerun its tests here).
    setUp = base.FilePending.setUp
    recorded = base.FilePending.recorded
    file = base.FilePending.file
    state = base.FilePending.state
    error = base.FilePending.error
    answered = staticmethod(base.FilePending.answered)

    def sweep(self, write, logged=None):
        with mock.patch.object(gh, "reviews", return_value=base.APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            return shadow_issues.file_pending(self.loop, db=self.db,
                                              log=(logged if logged is not None else []).append)


class CrossShadowDedup(Fixture):
    """#1978: one finding recorded by two shadow runs on one PR is filed once."""

    def test_in_one_sweep_one_post_and_the_second_is_a_duplicate(self):
        first, second = self.recorded(FIRST), self.recorded(SECOND)
        self.assertNotEqual(first, second)                    # two shadow runs
        filed, posts = self.file()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["title"], FIRST)
        self.assertEqual(len(filed), 1)
        self.assertEqual((self.state(first), self.state(second)), ("posted", "duplicate"))

    def test_in_separate_sweeps_the_second_makes_no_post(self):
        first = self.recorded(FIRST)
        self.assertEqual(len(self.file()[1]), 1)
        self.assertEqual(self.state(first), "posted")
        second = self.recorded(SECOND)                        # recorded after the first filed
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(second), "duplicate")

    def test_after_an_uncertain_first_filing_the_second_is_still_a_duplicate(self):
        first = self.recorded(FIRST)
        self.assertEqual(self.sweep(self.answered(502)), [])
        self.assertEqual(self.state(first), "uncertain")
        second = self.recorded(SECOND)
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(second), "duplicate")

    def test_filed_titles_include_a_shadows_filing(self):
        self.recorded(FIRST)
        self.file()
        with ledger.connect(self.db) as con:
            seat = con.execute("SELECT r.seat FROM filed_issues f JOIN runs r ON r.id=f.run_id"
                               ).fetchone()[0]
        self.assertEqual(seat, "shadow")
        self.assertEqual(self.sup.filed_titles(REPO, 7), [FIRST.casefold()])


class RefusalLog(Fixture):
    """#1984: each refused finding is named in the sweep's log, retried or dropped."""

    def test_a_retried_refusal_names_each_findings_title(self):
        self.recorded("Docs miss the flag")
        self.recorded("Retry loops forever")
        logged = []
        self.assertEqual(self.sweep(self.answered(403, "Resource not accessible"), logged), [])
        self.assertEqual(len(logged), 2)
        self.assertEqual(len(set(logged)), 2, logged)         # never the same line twice
        self.assertIn("#7 ('Docs miss the flag') not filed, retried next sweep: ", logged[0])
        self.assertIn("#7 ('Retry loops forever') not filed, retried next sweep: ", logged[1])

    def test_a_dropped_refusal_names_each_findings_title(self):
        runs = [self.recorded("Docs miss the flag"), self.recorded("Retry loops forever")]
        with ledger.connect(self.db) as con:                  # one refusal left before the drop
            con.execute("UPDATE shadow_issues SET attempts=?", (shadow_issues.MAX_ATTEMPTS - 1,))
        logged = []
        self.assertEqual(self.sweep(self.answered(403), logged), [])
        self.assertEqual([self.state(run) for run in runs], ["denied", "denied"])
        why = ": GitHub refused the issue write: HTTP 403"
        self.assertEqual(logged, [
            "shadow-found issue for #7 ('Docs miss the flag') not filed, dropped" + why,
            "shadow-found issue for #7 ('Retry loops forever') not filed, dropped" + why])


if __name__ == "__main__":
    unittest.main()
