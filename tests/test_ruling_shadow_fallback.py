"""Review on #2585: a ruling's copy of a finding the ruled shadow review also asked for is never
dropped before the shadow's copy is filed (#2209).

While the shadow's request waits to be filed (or its POST is under way), the ruling's waits.
Once the shadow's is filed (or may have been: uncertain, or a duplicate of the reviewer's), the
ruling's is a ``duplicate``. If the shadow's ends ``denied`` (refused past its attempts, or expired
before a real review), or the loop stops filing shadow requests, the ruling's is filed. Each case
fails without the change, which dropped the ruling's copy as soon as the shadow's was recorded.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros.state import ledger  # noqa: E402
import test_ruling_shadow_dedup as dedup  # noqa: E402

CITED = "diaktoros/history.py:48: false doc"


class Fallback(unittest.TestCase):
    setUp = dedup.FilePending.setUp
    recorded, file, state = dedup.FilePending.recorded, dedup.FilePending.file, dedup.FilePending.state
    ruled_over_shadow = dedup.FilePending.ruled_over_shadow

    def shadow_becomes(self, state):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state=? WHERE run_id='s1'", (state,))

    def test_the_rulings_copy_waits_while_the_shadows_is_still_to_be_filed(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "recorded")         # held, not ruled out

    def test_it_is_filed_once_the_shadows_copy_is_refused_for_good(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.shadow_becomes("posting")                        # its POST under way, then refused
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "recorded")
        self.assertEqual(self.sup.shadow_issue_refused("s1", 1, "HTTP 403", 1), "denied")
        filed, posts = self.file()
        self.assertEqual((filed, self.state(run)), ([201], "posted"))

    def test_it_is_filed_once_the_shadows_copy_expired_or_filing_was_turned_off(self):
        for name in ("expired", "turned off"):
            with self.subTest(name):
                self.setUp()
                run = self.ruled_over_shadow(CITED, CITED)
                self.file()
                if name == "expired":
                    self.shadow_becomes("denied")            # what WAIT_S's drop records
                else:
                    self.loop["shadow"] = {"profile": "p"}
                self.assertEqual(len(self.file()[1]), 1)
                self.assertEqual(self.state(run), "posted")

    def test_it_is_a_duplicate_once_the_shadows_copy_was_or_may_have_been_filed(self):
        for state in ("posted", "uncertain", "duplicate"):
            with self.subTest(state):
                self.setUp()
                run = self.ruled_over_shadow(CITED, CITED)
                self.file()
                self.shadow_becomes(state)
                self.assertEqual(self.file(), ([], []))
                self.assertEqual(self.state(run), "duplicate")

    def test_a_location_in_a_url_query_hides_no_other_finding(self):
        run = self.ruled_over_shadow("see https://example.com/?file=a.py:12 and real.py:90",
                                     "a.py:12: something else", shadow_state="posted")
        self.assertEqual(len(self.file()[1]), 1)
        self.assertEqual(self.state(run), "posted")

    def test_an_extensionless_path_is_the_same_place(self):
        run = self.ruled_over_shadow("Makefile:12 runs the wrong target",
                                     "the build rule at Makefile:10-14 is stale",
                                     shadow_state="posted")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "duplicate")


if __name__ == "__main__":
    unittest.main()
