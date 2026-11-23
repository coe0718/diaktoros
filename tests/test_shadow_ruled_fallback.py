"""#2603: the shadow sweep never re-files a finding a ruling over that shadow review filed.

With ``shadow.file_issues`` off, a ruling's copy of a finding its shadow review also asked for is
filed as the fallback (#2209, review of #2585, F1). Turned back on, the shadow sweep compared
titles only, so the shadow's own request for that finding, under another title, was filed too.
Now the sweep also reads the ruling's copy at the same PR and head (``ruling_issues.ruled_copy``):
a close title or the same place cited first in both bodies is that finding.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruling_shadow_dedup as dedup  # noqa: E402
import test_shadow_issues as base  # noqa: E402
from diaktoros import broker, gh, shadow_issues  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

CITED = "diaktoros/history.py:48: the doc line is false"


class RuledFallback(unittest.TestCase):
    setUp = dedup.FilePending.setUp
    recorded, file, state = dedup.FilePending.recorded, dedup.FilePending.file, dedup.FilePending.state
    ruled_over_shadow = dedup.FilePending.ruled_over_shadow

    def shadow_sweep(self):
        """The shadow's own sweep, with ``shadow.file_issues`` on: what it POSTed."""
        self.loop = {**self.loop, "reviewer_seat": "review", "reviewers": ["review"],
                     "shadow": {"profile": "p", "file_issues": True}}
        posts = []

        def write(loop, path, method, body, login):
            posts.append(body["title"])
            return {"number": 300 + len(posts), "labels": [{"name": x} for x in body["labels"]]}, ""
        with mock.patch.object(gh, "reviews", return_value=base.APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            shadow_issues.file_pending(self.loop, db=self.db)
        return posts

    def filed_while_off(self, shadow_body=CITED):
        """The issue's scenario: a ruling's fallback filed while the loop files no shadow's."""
        run = self.ruled_over_shadow("`diaktoros/history.py:44-50` promises exit 2", shadow_body)
        self.loop["shadow"] = {"profile": "p"}                   # shadow.file_issues off
        self.assertEqual(len(self.file()[1]), 1)
        self.assertEqual(self.state(run), "posted")
        return run

    def shadow_state(self):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT state FROM shadow_issues WHERE run_id='s1'").fetchone()[0]

    def test_turned_back_on_the_shadow_sweep_does_not_file_the_rulings_finding_again(self):
        self.filed_while_off()
        self.assertEqual(self.shadow_sweep(), [])
        self.assertEqual(self.shadow_state(), "duplicate")

    def test_a_ruling_copy_that_may_have_been_filed_drops_it_too(self):
        run = self.filed_while_off()
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state='uncertain' WHERE run_id=?", (run,))
        self.assertEqual(self.shadow_sweep(), [])
        self.assertEqual(self.shadow_state(), "duplicate")

    def test_a_ruling_copy_still_being_sent_holds_it(self):
        run = self.filed_while_off()
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state='posting', updated=? WHERE run_id=?",
                        (time.time(), run))
        self.assertEqual(self.shadow_sweep(), [])
        self.assertEqual(self.shadow_state(), "recorded")         # waits for that one's outcome

    def test_a_ruling_copy_still_to_be_filed_never_holds_it(self):
        # The ruling's copy waits on the shadow's (#2209), so the shadow's must not wait on it.
        self.ruled_over_shadow("`diaktoros/history.py:44-50` promises exit 2", CITED)
        self.assertEqual(self.shadow_sweep(), [dedup.SHADOW_TITLE])
        self.assertEqual(self.shadow_state(), "posted")

    def test_a_different_place_is_still_filed(self):
        self.filed_while_off(shadow_body="diaktoros/explain.py:48: another finding")
        self.assertEqual(self.shadow_sweep(), [dedup.SHADOW_TITLE])
        self.assertEqual(self.shadow_state(), "posted")


if __name__ == "__main__":
    unittest.main()
