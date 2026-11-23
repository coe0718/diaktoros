"""Review on #2585 (round 2): a ruling's copy that waits on the ruled shadow's request always ends.

F1: a shadow POST whose sweep was stopped after its claim stayed ``posting`` for ever, and the
ruling's copy waited on it for ever. ``shadow_issues.settle_stale`` ends that claim past
``POSTING_STALE_S``: back to ``recorded`` when its ``filed_issues`` row (committed before the POST)
is missing, so it was never sent; ``uncertain`` otherwise, never replayed. F5: ``unlabelled`` is a
filed issue. F6: a settled shadow outcome counts as filed whatever the loop's settings now; only a
request still to be filed falls back when the loop stops filing a shadow's.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruling_shadow_dedup as dedup  # noqa: E402
import test_shadow_issues as shadow_base  # noqa: E402
from diaktoros import broker, gh, ruling_issues, shadow_issues  # noqa: E402
from diaktoros.broker import filing as broker_filing  # noqa: E402
from diaktoros.ruled_approval import POSTING_STALE_S  # noqa: E402
from diaktoros.run_supervisor import Supervisor, shadow_filings, turn_prompts  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

CITED = "diaktoros/history.py:48: false doc"
LATER = POSTING_STALE_S + 60             # a sweep this long after the claim (the bound reused)


class Crash(BaseException):
    """The sweep's process stopped: not an ``Exception``, so nothing in the sweep catches it."""


class Base(unittest.TestCase):
    setUp_ = dedup.FilePending.setUp
    recorded, file, state = dedup.FilePending.recorded, dedup.FilePending.file, dedup.FilePending.state
    ruled_over_shadow = dedup.FilePending.ruled_over_shadow

    def setUp(self):
        self.setUp_()
        self.loop.update(reviewer_seat="review", reviewers=["review"])
        self.logged = []

    def shadow_becomes(self, state):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state=? WHERE run_id='s1'", (state,))

    def shadow_sweep(self, crash=None, later=0):
        """The shadow's own sweep; ``crash`` names the step its process is stopped at."""
        posts = []

        def write(loop, path, method, body, login):
            posts.append(body)
            return {"number": 300 + len(posts), "labels": [{"name": x} for x in body["labels"]]}, ""
        settled = Supervisor.shadow_issue_status

        def stop_at_outcome(sup, run_id, seq, state, **kwargs):
            if state in ("posted", "unlabelled"):
                raise Crash
            return settled(sup, run_id, seq, state, **kwargs)
        stop = {"post": mock.patch.object(broker_filing, "file_shadow_issue", side_effect=Crash),
                "claim": mock.patch.object(Supervisor, "record_shadow_filing", side_effect=Crash),
                "outcome": mock.patch.object(Supervisor, "shadow_issue_status", stop_at_outcome),
                None: contextlib.nullcontext()}[crash]
        with mock.patch.object(gh, "reviews", return_value=shadow_base.APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"), stop:
            try:
                shadow_issues.file_pending(self.loop, db=self.db, now=time.time() + later,
                                           log=self.logged.append)
            except Crash:
                pass
        return posts

    def ruling_sweep(self, later=0):
        posts = []

        def write(loop, path, method, body, login):
            posts.append(body)
            return {"number": 200 + len(posts), "labels": [{"name": x} for x in body["labels"]]}, ""
        with mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            ruling_issues.file_pending(self.loop, db=self.db, now=time.time() + later,
                                       log=self.logged.append)
        return posts

    def shadow_row(self):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute("SELECT state, attempts FROM shadow_issues WHERE run_id='s1'"
                               ).fetchone()

    def filed_row(self):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute("SELECT state FROM filed_issues WHERE run_id='s1'").fetchall()


class StaleClaim(Base):
    """F1: the shadow's sweep stopped after claiming its POST."""

    def test_stopped_after_the_post_may_have_been_sent_it_is_uncertain_and_the_ruling_a_duplicate(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.assertEqual(self.shadow_sweep(crash="post"), [])
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("posting", [("recorded",)]))
        self.assertEqual(self.ruling_sweep(), [])                  # a live claim: waited on
        self.assertEqual(self.state(run), "recorded")
        self.assertEqual(self.ruling_sweep(later=LATER), [])       # never a second POST
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("uncertain", [("uncertain",)]))
        self.assertEqual(self.state(run), "duplicate")
        self.assertTrue(any("shadow-found issue for #7" in line and "uncertain" in line
                            for line in self.logged), self.logged)
        self.assertEqual(self.shadow_sweep(later=LATER), [])       # nor from the shadow's sweep

    def test_stopped_before_the_post_it_is_filed_later_once_and_the_ruling_waits_for_it(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.assertEqual(self.shadow_sweep(crash="claim"), [])
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("posting", []))
        self.assertEqual(self.ruling_sweep(later=LATER), [])
        self.assertEqual(self.shadow_row(), ("recorded", 1))       # never sent: a refusal
        self.assertEqual(self.state(run), "recorded")               # still waits on it
        self.assertEqual(len(self.shadow_sweep()), 1)
        self.assertEqual(self.shadow_row()[0], "posted")
        self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual(self.state(run), "duplicate")

    def test_stopped_before_the_post_with_filing_off_the_rulings_copy_is_filed(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.shadow_sweep(crash="claim")
        self.loop["shadow"] = {"profile": "p"}                      # no shadow sweep runs now
        self.assertEqual(len(self.ruling_sweep(later=LATER)), 1)
        self.assertEqual((self.shadow_row()[0], self.state(run)), ("recorded", "posted"))

    def test_stopped_after_github_filed_it_it_is_unlabelled_with_its_number(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.assertEqual(len(self.shadow_sweep(crash="outcome")), 1)
        self.assertEqual((self.shadow_row()[0], self.filed_row()), ("posting", [("posted",)]))
        self.assertEqual(self.ruling_sweep(later=LATER), [])
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT state, issue_number FROM shadow_issues WHERE "
                                         "run_id='s1'").fetchone(), ("unlabelled", 301))
        self.assertEqual(self.state(run), "duplicate")

    def test_the_shadows_own_sweep_ends_it_too(self):
        self.ruled_over_shadow(CITED, CITED)
        self.shadow_sweep(crash="claim")
        self.assertEqual(self.shadow_sweep(), [])                   # not stale yet: left alone
        self.assertEqual(self.shadow_row()[0], "posting")
        self.assertEqual(len(self.shadow_sweep(later=LATER)), 1)
        self.assertEqual(self.shadow_row()[0], "posted")

    def test_one_stopped_before_the_post_every_time_is_dropped_at_the_attempt_bound(self):
        self.ruled_over_shadow(CITED, CITED)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET attempts=? WHERE run_id='s1'",
                        (shadow_issues.MAX_ATTEMPTS - 1,))
        self.shadow_sweep(crash="claim")
        self.ruling_sweep(later=LATER)
        self.assertEqual(self.shadow_row(), ("denied", shadow_issues.MAX_ATTEMPTS))


class States(Base):
    """F5 and F6: every state a shadow request ends in, read as the filing and the prompt read it."""

    EXPECTED = {"recorded": "waiting", "posting": "waiting", "posted": "filed",
                "unlabelled": "filed", "uncertain": "maybe", "duplicate": "filed", "denied": None}

    def test_the_states_the_shadows_filing_writes_are_each_classified(self):
        source = Path(shadow_issues.__file__).read_text() + Path(shadow_filings.__file__).read_text()
        for state in self.EXPECTED:
            self.assertRegex(source, f"[\"']{state}[\"']")            # a state it really writes
            with self.subTest(state):
                self.assertEqual(ruling_issues._standing(state, True), self.EXPECTED[state])

    def test_an_unlabelled_shadow_issue_makes_the_rulings_copy_a_duplicate(self):
        run = self.ruled_over_shadow(CITED, CITED)
        self.shadow_becomes("unlabelled")
        self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual(self.state(run), "duplicate")

    def test_a_settled_shadow_outcome_counts_with_shadow_filing_turned_off(self):
        for state in ("posted", "unlabelled", "uncertain", "duplicate"):
            with self.subTest(state):
                self.setUp()
                run = self.ruled_over_shadow(CITED, CITED, shadow_state=state)
                self.loop["shadow"] = {"profile": "p"}
                self.assertEqual(self.ruling_sweep(), [])
                self.assertEqual(self.state(run), "duplicate")

    def test_with_filing_off_a_recorded_one_falls_back_but_a_live_post_is_waited_on(self):
        for state, posts, after in (("recorded", 1, "posted"), ("posting", 0, "recorded")):
            with self.subTest(state):
                self.setUp()
                run = self.ruled_over_shadow(CITED, CITED, shadow_state=state)
                with ledger.connect(self.db) as con:      # a claim this sweep's moment made
                    con.execute("UPDATE shadow_issues SET updated=? WHERE run_id='s1'",
                                (time.time(),))
                self.loop["shadow"] = {"profile": "p"}
                self.assertEqual(len(self.ruling_sweep()), posts)
                self.assertEqual(self.state(run), after)


class Prompt(dedup.escalate.Base):
    def test_unlabelled_is_filed_and_filed_ones_are_listed_with_shadow_filing_off(self):
        self.loop = {**self.loop, "adjudicator_file_issues": True,
                     "shadow": {**self.loop["shadow"], "file_issues": True}}
        row = {"repo": dedup.escalate.fg.REPO, "pr": 7, "head": dedup.escalate.fg.HEAD}
        self.shadow("REQUEST_CHANGES")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.executemany(
                "INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,labels,"
                "state,created,updated) VALUES('s1',?,?,7,?,?,?,'b','[]',?,1,1)",
                [(seq, row["repo"], row["head"], title, f"k{seq}", state)
                 for seq, title, state in ((1, "Unlabelled one", "unlabelled"),
                                           (2, "Still due", "recorded"))])
        filed, waiting = turn_prompts.shadow_review_section(self.loop, row).split("not filed yet")
        self.assertIn("- Unlabelled one", filed)
        self.assertNotIn("Unlabelled one", waiting)
        off = {**self.loop, "shadow": {**self.loop["shadow"], "file_issues": False}}
        text = turn_prompts.shadow_review_section(off, row)
        self.assertIn("the host filed them from this PR", text)
        self.assertIn("- Unlabelled one", text)
        self.assertNotIn("Still due", text)                         # never filed now: ask for it


if __name__ == "__main__":
    unittest.main()
