"""Review on #2585 (F9): a shadow's POST settled ``uncertain`` (its outcome unknown) may yet be
refused for good. The adjudicator's prompt said the host filed it and not to ask again, and the
recording guard refused the adjudicator's same title, so after that refusal nothing was left to
file: the finding was lost. Now a maybe-filed request (``uncertain``, or a copy dropped on one,
``MAYBE``) is listed apart, as one whose filing is unknown; the adjudicator's same title is kept
beside it as a fallback, dropped while the unknown stands (never a replayed write) and filed once
a late refusal shows the shadow's never was. Drives the sqlite ledger, the real prompt, guard and
sweeps end to end; only GitHub is mocked.
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
import test_ruling_shadow_interleave as interleave  # noqa: E402
import test_ruling_shadow_settle as settle  # noqa: E402
from diaktoros import run_supervisor, shadow_issues  # noqa: E402
from diaktoros.run_supervisor import shadow_filings, turn_prompts  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

REPO, HEAD, TITLE = dedup.REPO, dedup.HEAD, dedup.SHADOW_TITLE
UNKNOWN = "whether the host's filing went through is unknown"


class MaybeFiled(interleave.Interleave):
    def setUp(self):
        super().setUp()
        self.loop["shadow"] = {"profile": "p", "file_issues": True}
        dedup.shadow_asked(self.db, "s1", TITLE, settle.CITED)

    def prompt(self):
        """What the adjudicator ruling over ``s1`` is told about its requests."""
        with mock.patch.object(run_supervisor, "production_ledger", return_value=self.db):
            return turn_prompts.shadow_requests_section(
                self.loop, {"repo": REPO, "pr": 7, "head": HEAD}, "s1")

    def adjudicator_asks(self):
        """``a1`` asks for the same title through the real guard, then rules over ``s1``."""
        now = time.time()
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated,launch_intent) VALUES('a1','da',?,7,?,'adjudicator',"
                        "'running',0,'ta',?,?,?)", (REPO, HEAD, now, now, now))
        self.sup.record_shadow_issue("a1", REPO, 7, HEAD, TITLE, settle.CITED, ["bug"], 3,
                                     seat="adjudicator", shadow_files=True)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded' WHERE id='a1'")
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated,shadow_run) VALUES('a1',?,7,?,'ta','ACCEPT','why',?,?,'s1')",
                        (REPO, HEAD, now, now))
            return con.execute("SELECT title_key FROM shadow_issues WHERE run_id='a1'"
                               ).fetchone()[0]

    def assert_listed_as_unknown(self, text):
        filed, _, unknown = text.partition(UNKNOWN)
        self.assertIn(f"- {TITLE}", unknown)
        self.assertNotIn(TITLE, filed)                  # never "filed: do not ask again"

    def test_a_post_settled_unknown_then_refused_files_the_adjudicators_copy_once(self):
        self.shadow_sweep(crash="post")                 # s1's POST under way, its sweep stopped
        shadow_issues.settle_stale(self.sup, REPO, time.time() + settle.LATER, self.logged.append)
        self.assertEqual(self.shadow_row()[0], "uncertain")
        self.assert_listed_as_unknown(self.prompt())
        self.assertEqual(self.adjudicator_asks(), f"{' '.join(TITLE.casefold().split())}"
                                                  "#fallback:a1")
        self.assertEqual(self.ruling_sweep(), [])       # the unknown stands: no second write
        self.assertEqual(self.state("a1"), "duplicate")
        self.late_refusal("s1")                         # the shadow's POST was refused for good
        self.assertEqual((self.shadow_row()[0], self.state("a1")), ("denied", "recorded"))
        self.assertEqual([post["title"] for post in self.ruling_sweep()], [TITLE])
        self.assertEqual(self.state("a1"), "posted")
        self.assertEqual((self.ruling_sweep(), self.shadow_sweep()), ([], []))

    def test_a_copy_dropped_on_an_unknown_is_listed_and_kept_beside_as_one(self):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state='duplicate', error=? WHERE run_id='s1'",
                        (shadow_filings.MAYBE,))
        self.assert_listed_as_unknown(self.prompt())
        self.assertTrue(self.adjudicator_asks().endswith("#fallback:a1"))


if __name__ == "__main__":
    unittest.main()
