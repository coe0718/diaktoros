"""Review on #2585 (round 3): a ruling's fallback survives the recording guard and a live shadow
POST, and a delayed sweep whose claim was ended never POSTs.

F1: a ruling's copy whose title is close to a shadow POST still under way waits for its outcome
(it was ``duplicate`` at once, so a final refusal left neither filed), and the recording guard
takes the adjudicator's same-title request as that fallback (it refused it, so the fallback the
prompt asks for was never recorded). F7: ``settle_stale`` ends a claim past ``POSTING_STALE_S``, but
its sweep may only be delayed; resumed, it found nothing fencing it and POSTed a second issue.
Each drives the sqlite ledger, the recording guard and both sweeps; only GitHub is mocked.
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
import test_ruling_shadow_settle as settle  # noqa: E402
import test_shadow_issues as shadow_base  # noqa: E402
from diaktoros import broker, gh, shadow_issues  # noqa: E402
from diaktoros.broker import filing as broker_filing  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

REPO, HEAD, LATER = dedup.REPO, dedup.HEAD, settle.LATER
CLOSE = "history --since: the new exit-2 doc line is false when the window is empty"


class Base(settle.Base):
    def shadow_post(self, send):
        """The shadow's sweep, its POST replaced by ``send`` (GitHub's side only)."""
        posts = []

        def write(loop, path, method, body, login):
            posts.append(body)
            return {"number": 300 + len(posts), "labels": [{"name": x} for x in body["labels"]]}, ""
        with mock.patch.object(gh, "reviews", return_value=shadow_base.APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"), \
                mock.patch.object(broker_filing, "file_shadow_issue", side_effect=send):
            shadow_issues.file_pending(self.loop, db=self.db, log=self.logged.append)
        return posts

    def last_try(self):
        """The shadow's next refusal is its last (``MAX_ATTEMPTS``)."""
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET attempts=? WHERE run_id='s1'",
                        (shadow_issues.MAX_ATTEMPTS - 1,))

    def refused_mid_post(self):
        """The ruling's sweep runs while the shadow's POST is under way; then GitHub refuses it,
        for good. What that sweep posted, and the ruling's state then."""
        seen = []

        def send(loop, **_kwargs):
            seen.append((self.ruling_sweep(), self.state(self.run)))
            raise broker.BrokerNotSent("HTTP 403 refused")
        self.last_try()
        self.assertEqual(self.shadow_post(send), [])
        return seen


class InFlight(Base):
    """F1: a close, not identical, title under way is no reason to drop the ruling's copy."""

    def test_a_close_title_mid_post_then_refused_for_good_files_the_rulings_copy(self):
        for ruled in (True, False):                   # over that shadow; or another's title only
            with self.subTest(ruled_over_the_shadow=ruled):
                self.setUp()
                self.run = self.ruled_over_shadow("diaktoros/explain.py:9: other place",
                                                  "diaktoros/history.py:48: false doc")
                with ledger.connect(self.db) as con:
                    con.execute("UPDATE shadow_issues SET title=?, title_key=? WHERE run_id=?",
                                (CLOSE, CLOSE.casefold(), self.run))
                    if not ruled:
                        con.execute("UPDATE rulings SET shadow_run=NULL WHERE run_id=?",
                                    (self.run,))
                self.assertEqual(self.refused_mid_post(), [([], "recorded")])   # waited
                self.assertEqual(self.shadow_row(), ("denied", shadow_issues.MAX_ATTEMPTS))
                self.assertEqual([post["title"] for post in self.ruling_sweep()], [CLOSE])
                self.assertEqual(self.state(self.run), "posted")


class SameTitle(Base):
    """F1: the adjudicator's same-title request is recorded by the real guard, and filed only if
    the shadow's never is."""

    def fallback(self):
        self.loop["shadow"] = {"profile": "p", "file_issues": True}
        dedup.shadow_asked(self.db, "s1", dedup.SHADOW_TITLE, settle.CITED)
        now = time.time()
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated,launch_intent) VALUES('a1','da',?,7,?,'adjudicator',"
                        "'running',0,'ta',?,?,?)", (REPO, HEAD, now, now, now))
        self.sup.record_shadow_issue("a1", REPO, 7, HEAD, dedup.SHADOW_TITLE, settle.CITED,
                                     ["bug"], 3, seat="adjudicator", shadow_files=True)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded' WHERE id='a1'")
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated,shadow_run) VALUES('a1',?,7,?,'ta','ACCEPT','why',?,?,'s1')",
                        (REPO, HEAD, now, now))
        self.run = "a1"

    def test_refused_for_good_mid_post_the_fallback_is_filed(self):
        self.fallback()
        self.assertEqual(self.ruling_sweep(), [])                  # still to be filed: waits
        self.assertEqual(self.refused_mid_post(), [([], "recorded")])
        self.assertEqual([post["title"] for post in self.ruling_sweep()], [dedup.SHADOW_TITLE])
        self.assertEqual(self.state("a1"), "posted")

    def test_filed_by_the_shadow_the_fallback_is_a_duplicate(self):
        self.fallback()
        self.assertEqual(len(self.shadow_sweep()), 1)
        self.assertEqual(self.ruling_sweep(), [])
        self.assertEqual(self.state("a1"), "duplicate")


class DelayedWorker(Base):
    """F7: a sweep paused after its claim, not stopped: its claim is ended meanwhile."""

    def pause_filing(self, meanwhile):
        original, seen = Supervisor.record_shadow_filing, []

        def paused(sup, *args, **kwargs):
            if not seen:                              # only the first sweep's is paused
                seen.append(None)
                seen[0] = meanwhile()
            return original(sup, *args, **kwargs)
        return mock.patch.object(Supervisor, "record_shadow_filing", paused), seen

    def test_a_resumed_shadow_sweep_never_posts_after_the_rulings_fallback(self):
        self.run = self.ruled_over_shadow(settle.CITED, settle.CITED)

        def meanwhile():
            self.loop["shadow"] = {"profile": "p"}                 # filing turned off
            return self.ruling_sweep(later=LATER), self.shadow_row(), self.state(self.run)
        pause, seen = self.pause_filing(meanwhile)
        with pause:
            resumed = self.shadow_sweep()
        self.assertEqual(len(seen[0][0]), 1)                       # the ruling's, posted
        self.assertEqual(seen[0][1:], (("recorded", 1), "posted"))
        self.assertEqual(resumed, [])                              # one issue in all
        self.assertEqual((self.shadow_row(), self.filed_row()), (("recorded", 1), []))
        self.assertTrue(any("its claim was ended" in line for line in self.logged), self.logged)

    def test_a_resumed_ruling_sweep_leaves_the_one_its_successor_posted(self):
        self.run = self.recorded(dedup.RULING_TITLE)
        pause, seen = self.pause_filing(lambda: self.ruling_sweep(later=LATER))
        with pause:
            resumed = self.ruling_sweep()
        self.assertEqual((len(seen[0]), resumed), (1, []))
        self.assertEqual(self.state(self.run), "posted")


if __name__ == "__main__":
    unittest.main()
