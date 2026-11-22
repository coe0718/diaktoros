"""#2209: a ruling over a shadow review does not re-file what that shadow review asked for.

Two models' one-line titles for one finding rarely pass the title match, so an adjudicator's
follow-up is also a ``duplicate`` when the ruled shadow review (``rulings.shadow_run``) asked, at
the same PR and head, for an issue whose body cites first the same file with overlapping lines.
Only where the loop files a shadow's requests. The ruling's prompt lists the shadow's requested
titles, so the adjudicator asks only for findings beyond them.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruling_issues as rulings  # noqa: E402
import test_shadow_escalate_ruling as escalate  # noqa: E402
from diaktoros import ruling_issues  # noqa: E402
from diaktoros.state import ledger  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

REPO, HEAD = rulings.REPO, rulings.HEAD
SHADOW_TITLE = "history --since: new exit-2 doc line is false when the window is empty"
RULING_TITLE = "history --since: doc promises exit 2 for an --out that is a file"


def shadow_asked(db, run, title, body, head=HEAD, state="recorded", repo=REPO):
    """A shadow run that asked to file one issue (``title``, ``body``) at ``head``."""
    with closing(sqlite3.connect(db)) as con, con:
        con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                    "created,updated) VALUES(?,?,?,7,?,'shadow','succeeded',0,?,1,1)",
                    (run, f"d-{run}", repo, head, f"t-{run}"))
        con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                    "labels,state,created,updated) VALUES(?,1,?,7,?,?,?,?,'[\"bug\"]',?,1,1)",
                    (run, repo, head, title, " ".join(title.casefold().split()), body, state))


class FilePending(unittest.TestCase):
    # The ruling FilePending fixture's plumbing, borrowed (inheriting would rerun its tests).
    setUp = rulings.FilePending.setUp
    recorded = rulings.FilePending.recorded
    file = rulings.FilePending.file
    state = rulings.FilePending.state

    def ruled_over_shadow(self, body, shadow_body, shadow_head=HEAD, shadow_state="recorded"):
        """An ACCEPT follow-up whose ruling was over a shadow run that asked for an issue."""
        self.loop["shadow"] = {"profile": "p", "file_issues": True}
        shadow_asked(self.db, "s1", SHADOW_TITLE, shadow_body, head=shadow_head,
                     state=shadow_state)
        run = self.recorded(RULING_TITLE)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE rulings SET shadow_run='s1' WHERE run_id=?", (run,))
            con.execute("UPDATE shadow_issues SET body=? WHERE run_id=?", (body, run))
        return run

    def test_the_same_place_cited_is_a_duplicate_whatever_the_title(self):
        run = self.ruled_over_shadow(
            "`diaktoros/history.py:40-52` says exit 2, but an empty window exits 0",
            "The doc line at diaktoros/history.py:48 is false when --out names a file",
            shadow_state="posted")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "duplicate")

    def test_a_shorter_path_to_the_same_file_is_the_same_place(self):
        run = self.ruled_over_shadow("history.py:50: exit 2 is promised",
                                     "diaktoros/history.py:48-52: the doc line is false",
                                     shadow_state="posted")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "duplicate")

    def test_a_close_title_the_shadow_asked_for_is_a_duplicate(self):
        run = self.ruled_over_shadow("no location", "none either", shadow_state="posted")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET title=? WHERE run_id=?",
                        (SHADOW_TITLE + ".", run))
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "duplicate")

    def test_a_different_place_is_still_posted(self):
        # Beside a same-place copy that is dropped, so the selection itself is what is tested:
        # without it both rows are posted.
        for name, body in (("other lines", "diaktoros/history.py:90: the doc is wrong"),
                           ("other file", "diaktoros/explain.py:48: the doc is wrong"),
                           ("no place", "the doc is wrong")):
            with self.subTest(name):
                self.setUp()
                run = self.ruled_over_shadow("diaktoros/history.py:44: exit 2 is false",
                                             "diaktoros/history.py:40-52: false doc",
                                             shadow_state="posted")
                with ledger.connect(self.db) as con:
                    con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,"
                                "title_key,body,labels,state,created,updated) VALUES(?,2,?,7,?,"
                                "'Another finding','another finding',?,'[]','recorded',1,1)",
                                (run, REPO, HEAD, body))
                filed, posts = self.file()
                self.assertEqual([post[1]["title"] for post in posts], ["Another finding"])
                with ledger.connect(self.db) as con:
                    self.assertEqual(con.execute("SELECT seq, state FROM shadow_issues WHERE "
                                                 "run_id=? ORDER BY seq", (run,)).fetchall(),
                                     [(1, "duplicate"), (2, "posted")])

    def test_only_the_ruled_shadow_at_the_same_head_and_still_to_be_filed_counts(self):
        cited = "diaktoros/history.py:48: false doc"
        for name, kwargs in (("another head", {"shadow_head": "f" * 40}),
                             ("denied", {"shadow_state": "denied"})):
            with self.subTest(name):
                self.setUp()
                run = self.ruled_over_shadow(cited, cited, **kwargs)
                self.assertEqual(len(self.file()[1]), 1)
                self.assertEqual(self.state(run), "posted")
        with self.subTest("another shadow run, not the ruled one"):
            self.setUp()
            run = self.ruled_over_shadow(cited, "nothing cited")
            shadow_asked(self.db, "s2", "Other title", cited)
            self.assertEqual(len(self.file()[1]), 1)
            self.assertEqual(self.state(run), "posted")

    def test_a_loop_that_never_files_a_shadows_requests_still_posts(self):
        cited = "diaktoros/history.py:48: false doc"
        run = self.ruled_over_shadow(cited, cited, shadow_state="posted")
        self.loop["shadow"] = {"profile": "p"}                # shadow filing off
        self.assertEqual(len(self.file()[1]), 1)
        self.assertEqual(self.state(run), "posted")

    def test_a_cited_location_is_never_read_from_a_url(self):
        self.assertIsNone(ruling_issues._cited("see https://github.com/o/r/blob/x/a.py:12"))
        self.assertEqual(ruling_issues._cited("https://example.com/?file=a.py:12 and src/b.py:99"),
                         ("src/b.py", 99, 99))            # the query string is the URL's too
        self.assertEqual(ruling_issues._cited("at 12:30, Makefile:5-7 runs it"),
                         ("Makefile", 5, 7))              # extensionless; digits name no file
        self.assertEqual(ruling_issues._cited("at `pkg/a.b.py:9-3`, and c.py:1"),
                         ("pkg/a.b.py", 3, 9))


class Prompt(escalate.Base):
    def asked(self, run, title, state="recorded"):
        shadow_asked(self.db, run, title, "body", state=state, repo=escalate.fg.REPO)

    def test_a_ruling_over_a_shadow_lists_what_it_asked_to_file(self):
        # With no shadow, and with a shadow that asked for nothing, none is listed; once the
        # shadow asked, its filed titles are not to be asked again, its unfiled ones still may.
        self.loop = {**self.loop, "adjudicator_file_issues": True,
                     "shadow": {**self.loop["shadow"], "file_issues": True}}
        row = {"repo": escalate.fg.REPO, "pr": 7, "head": escalate.fg.HEAD}
        self.assertNotIn("asked to file", turn_prompts.shadow_review_section(self.loop, row))
        self.shadow("REQUEST_CHANGES")
        self.assertNotIn("asked to file", turn_prompts.shadow_review_section(self.loop, row))
        self.asked("s9", "Another run")                       # another run's never shows
        with closing(sqlite3.connect(self.db)) as con, con:   # s1's run row exists already
            con.executemany(
                "INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,labels,"
                "state,created,updated) VALUES('s1',?,?,7,?,?,?,'b','[]',?,1,1)",
                [(seq, escalate.fg.REPO, escalate.fg.HEAD, title, f"k{seq}", state)
                 for seq, title, state in ((1, SHADOW_TITLE, "posted"), (2, "Waits", "recorded"),
                                           (3, "Gone", "denied"))])
        text = turn_prompts.shadow_review_section(self.loop, row)
        filed, waiting = text.split("not filed yet")
        self.assertIn("the host filed them from this PR: do not ask for them again", filed)
        self.assertIn(f"- {SHADOW_TITLE}", filed)
        self.assertIn("if you rule one valid, ask for it too", waiting)
        self.assertIn("- Waits", waiting)
        for absent in ("Gone", "Another run"):                # never to be filed; not this run
            self.assertNotIn(absent, text)
        off = {**self.loop, "shadow": {**self.loop["shadow"], "file_issues": False}}
        self.assertNotIn("asked to file", turn_prompts.shadow_review_section(off, row))

    def test_the_adjudicators_prompt_over_a_dispute_lists_none(self):
        from diaktoros import ci, gh, issue_facts
        from diaktoros.run_supervisor import pr_facts
        self.loop = {**self.loop, "adjudicator_file_issues": True,
                     "shadow": {**self.loop["shadow"], "file_issues": True}}
        self.shadow("REQUEST_CHANGES")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES('s1',1,?,7,?,?,'k','b','[]',"
                        "'recorded',1,1)", (escalate.fg.REPO, escalate.fg.HEAD, SHADOW_TITLE))
        row = {"repo": escalate.fg.REPO, "pr": 7, "head": escalate.fg.HEAD, "seat": "adjudicator"}
        marker = self.mark_escalated()
        with mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(ci, "read", return_value=None), \
                mock.patch.object(ci, "section", return_value=""), \
                mock.patch.object(pr_facts, "pr_record", return_value="RECORD"):
            shadowed = turn_prompts.isolated_prompt(self.loop, row, list(escalate.APPROVED),
                                                    marker=marker)
            plain = turn_prompts.isolated_prompt(self.loop, row, list(escalate.APPROVED),
                                                 marker={**marker, "escalated": {"by": "dispute"}})
        self.assertIn(f"- {SHADOW_TITLE}", str(shadowed))
        self.assertNotIn(SHADOW_TITLE, str(plain))


if __name__ == "__main__":
    unittest.main()
