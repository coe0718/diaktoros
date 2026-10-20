"""#1004 part 1: with ``shadow.escalate`` on, a shadow review holds the PR.

A PR is not ready to merge while the shadow review of its head is still running, or after that
review requested changes; ``ready``, the ready notice and ``explain`` say which. A head with no
shadow review to wait for (none queued, or a run that ended without one) is never held, so a
failed shadow never parks a PR for good; an unreadable ledger holds (unknown is not ready).
Escalation to the adjudicator (part 2) and upheld findings to the fixer (part 3) come later.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_ready as tr  # noqa: E402
import test_token_file_settings as tf  # noqa: E402
from diaktoros import config, gate  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

RAW = {"id": "w", "repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"],
       "read_token": "rev",
       "seats": {"reviewer": {"profile": "rev", "route": "r"},
                 "fixer": {"profile": "fix", "route": "f"}}}
HEAD = "a" * 40


class Setting(unittest.TestCase):
    def test_off_by_default_a_bool_and_only_with_every_one(self):
        self.assertFalse(config.shadow_escalate(config.normalize(
            {**RAW, "shadow": {"profile": "critic"}})))
        on = config.normalize({**RAW, "shadow": {"profile": "critic", "every": 1, "escalate": True}})
        self.assertTrue(config.shadow_escalate(on))
        for shadow, word in (({"profile": "critic", "escalate": True}, "every 1"),
                             ({"profile": "critic", "every": 2, "escalate": True}, "every 1"),
                             ({"profile": "critic", "every": 1, "escalate": "yes"}, "true or false")):
            with self.subTest(shadow=shadow), self.assertRaisesRegex(config.ConfigError, word):
                config.normalize({**RAW, "shadow": shadow})

    def test_the_form_block_turns_it_on_and_off(self):
        on = settings_form.shadow_block({"profile": "critic", "every": 1}, None, None, "t",
                                        escalate="on")
        self.assertEqual(on, {"profile": "critic", "every": 1, "escalate": True})
        off = settings_form.shadow_block(on, None, None, "t", escalate="off")
        self.assertNotIn("escalate", off)
        # "off" with no shadow at all changes nothing (a form that always sends it is harmless);
        # "on" needs a profile.
        self.assertEqual(settings_form.shadow_block({}, None, None, "t", escalate="off"), {})
        with self.assertRaisesRegex(config.ConfigError, "needs a shadow profile"):
            settings_form.shadow_block({}, None, None, "t", escalate="on")
        with self.assertRaisesRegex(config.ConfigError, "on or off"):
            settings_form.shadow_block({"profile": "critic"}, None, None, "t", escalate="maybe")


class SetCommand(unittest.TestCase):
    setUp = tf.TokenFileSettingsTests.setUp
    pat = tf.TokenFileSettingsTests.pat
    run_cli = tf.TokenFileSettingsTests.run_cli
    init_argv = tf.TokenFileSettingsTests.init_argv
    loop_json = tf.TokenFileSettingsTests.loop_json
    loop_file = tf.TokenFileSettingsTests.loop_file

    def test_set_refuses_it_without_every_one_and_takes_it_with(self):
        rc, out = self.run_cli(self.init_argv("--shadow-profile", "critic"))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--shadow-escalate", "on"])
        self.assertNotEqual(rc, 0)
        self.assertIn("every 1", out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--shadow-every", "1",
                                "--shadow-escalate", "on"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.loop_json()["shadow"].get("escalate"))
        rc, out = self.run_cli(["set", "--loop", "widgets", "--shadow-escalate", "off"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("escalate", self.loop_json()["shadow"])
        # Shadow review of #1097: `--no-shadow --shadow-escalate on` dropped the "on" silently.
        rc, out = self.run_cli(["set", "--loop", "widgets", "--no-shadow", "--shadow-escalate", "on"])
        self.assertNotEqual(rc, 0)
        self.assertIn("needs a shadow profile", out)
        self.assertTrue(self.loop_json()["shadow"].get("profile"))     # nothing was written


class Hold(unittest.TestCase):
    """``gate.shadow_hold`` against a real run ledger."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = pathlib.Path(tmp.name) / "runs.sqlite"
        Supervisor(self.db)
        self.loop = config.normalize({**RAW, "shadow": {"profile": "critic", "every": 1,
                                                        "escalate": True}})
        self.n = 0

    def shadow_run(self, state, verdict=None, head=HEAD):
        self.n += 1
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                        (f"r{self.n}", f"d{self.n}", "acme/widgets", 7, head, "shadow", state,
                         f"t{self.n}", time.time() + self.n, time.time() + self.n))
            if verdict:
                con.execute("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,"
                            "authorized,denial,held,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (f"r{self.n}", "acme/widgets", 7, head, "m", verdict, "F1: a.py:1: x",
                             1, "", 0, time.time() + self.n))

    def hold(self):
        return gate.shadow_hold(self.loop, 7, HEAD, db=self.db)

    def test_a_running_or_pending_shadow_holds(self):
        for state in ("pending", "waiting", "running"):
            with self.subTest(state=state):
                self.shadow_run(state)
                held, note = self.hold()
                self.assertIn("waiting on the shadow review", held)
                self.assertEqual(note, "")

    def test_a_shadow_that_requested_changes_holds_and_one_that_approved_does_not(self):
        self.shadow_run("succeeded", "REQUEST_CHANGES")
        self.assertIn("requested changes", self.hold()[0])
        self.shadow_run("succeeded", "APPROVE")              # a later review at the head wins
        self.assertEqual(self.hold(), ("", ""))

    def test_a_shadow_run_queued_after_an_approving_review_holds_until_it_lands(self):
        # Shadow review of #1097 (P2): a re-review at the same head (a dispute's maintainer
        # re-request) queues a new shadow run; the earlier APPROVE must not hide it.
        self.shadow_run("succeeded", "APPROVE")
        self.assertEqual(self.hold(), ("", ""))
        self.shadow_run("running")
        self.assertIn("waiting on the shadow review", self.hold()[0])
        self.shadow_run("succeeded", "APPROVE")              # the re-review approved too
        self.assertEqual(self.hold(), ("", ""))

    def test_an_open_run_older_than_the_newest_review_is_not_waited_for(self):
        self.shadow_run("running")
        self.shadow_run("succeeded", "APPROVE")
        self.assertEqual(self.hold(), ("", ""))

    def test_nothing_to_wait_for_is_a_note_never_a_hold(self):
        held, note = self.hold()                              # none queued
        self.assertEqual(held, "")
        self.assertIn("no shadow review was queued", note)
        for state in ("failed", "cancelled", "succeeded", "uncertain"):   # ended without one
            with self.subTest(state=state):
                self.shadow_run(state)
                held, note = self.hold()
                self.assertEqual(held, "")
                self.assertIn(f"ended {state} without a review", note)

    def test_another_heads_shadow_never_holds_this_one(self):
        self.shadow_run("running", head="b" * 40)
        self.assertEqual(self.hold()[0], "")

    def test_a_missing_ledger_is_a_note_an_unreadable_one_holds_escalate_off_never_does(self):
        # Shadow review of #1097: a ledger never created holds no shadow review, so it is a
        # note, not a hold on every approved PR of a fresh loop. One that exists but can't be
        # read still holds.
        missing = self.db.with_name("missing.sqlite")
        held, note = gate.shadow_hold(self.loop, 7, HEAD, db=missing)
        self.assertEqual(held, "")
        self.assertIn("no run ledger yet", note)
        broken = self.db.with_name("broken.sqlite")
        broken.write_bytes(b"not a database at all" * 40)
        held, _ = gate.shadow_hold(self.loop, 7, HEAD, db=broken)
        self.assertIn("unreadable", held)
        off = config.normalize({**RAW, "shadow": {"profile": "critic", "every": 1}})
        self.shadow_run("running")
        self.assertEqual(gate.shadow_hold(off, 7, HEAD, db=self.db), ("", ""))


class ReadyAndExplain(unittest.TestCase):
    """The hold reaches ``hermes dk ready`` and ``explain``'s report. Borrows test_ready's
    harness without inheriting (and rerunning) its tests."""
    setUp = tr.Ready.setUp
    add = tr.Ready.add
    run_ready = tr.Ready.run_ready

    def test_ready_leaves_out_a_held_pr_and_says_why(self):
        self.add(1)
        self.add(2)

        def hold(loop, number, head, db=None):
            return (("waiting on the shadow review at " + head[:7] + " (running)", "")
                    if number == 2 else ("", ""))
        with mock.patch.object(gate, "shadow_hold", side_effect=hold):
            text = self.run_ready()
        self.assertIn("1. #1", text)
        self.assertIn("shadow review: waiting on the shadow review", text)
        self.assertNotIn("2. #2", text)

    def test_explain_names_the_hold_as_a_blocker_and_the_note_in_the_report(self):
        facts = {"pr": None, "pr_error": "", "reviews": [], "armed": True, "read_at": 0}
        report = gate.explain(self.loop, self.st, 7, {**facts,
                              "shadow_hold": ("", "no shadow review was queued for aaaaaaa")})
        self.assertEqual(report["shadow"]["note"], "no shadow review was queued for aaaaaaa")
        self.assertFalse(any("shadow review:" in b for b in report["blockers"]))


import test_fixer_gating as fg  # noqa: E402


class ExplainBlocker(fg.Base):
    def approved_facts(self, hold):
        verdict = {"id": 9, "state": "APPROVED", "commit_id": fg.HEAD,
                   "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer", "id": 2}}
        return {"pr": fg.LIVE, "reviews": [verdict], "armed": True, "read_at": 0,
                "shadow_hold": (hold, "")}

    def test_explain_never_recommends_merging_a_held_approved_head(self):
        # Shadow review of #1097 (F2): the blocker was listed, but `next` still said "approved; a
        # human merges it".
        held = gate.explain(self.loop, self.st, 7, self.approved_facts(
            "the shadow review requested changes at aaaaaaa"))
        self.assertEqual(held["next"]["kind"], "operator")
        self.assertIn("do not merge", held["next"]["action"])
        waiting = gate.explain(self.loop, self.st, 7, self.approved_facts(
            "waiting on the shadow review at aaaaaaa (running)"))
        self.assertEqual(waiting["next"]["kind"], "wait")
        free = gate.explain(self.loop, self.st, 7, self.approved_facts(""))
        self.assertIn("a human merges it", free["next"]["action"])

    def test_a_held_head_is_an_explain_blocker(self):
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0,
                 "shadow_hold": ("the shadow review requested changes at aaaaaaa", "")}
        report = gate.explain(self.loop, self.st, 7, facts)
        self.assertIn("shadow review: the shadow review requested changes at aaaaaaa",
                      report["blockers"])
        self.assertEqual(report["shadow"]["hold"], facts["shadow_hold"][0])


import test_merged_tree_blockers as mt  # noqa: E402


class Wiring(unittest.TestCase):
    """Shadow review of #1097 (F1): the explain_facts read and the CLI's note line, pinned."""

    def test_explain_facts_asks_shadow_hold_about_the_live_head(self):
        with mock.patch.object(gate.gh, "fetch", side_effect=[(mt.PR, ""), ([], "")]), \
                mock.patch.object(gate, "hooks_read", return_value=(True, "")), \
                mock.patch.object(gate, "shadow_hold", return_value=("held here", "")) as hold:
            facts = gate.explain_facts(mt.LOOP, 7)
        hold.assert_called_once_with(mt.LOOP, 7, mt.HEAD)
        self.assertEqual(facts["shadow_hold"], ("held here", ""))

    def test_the_explain_command_prints_the_note(self):
        # The whole command, real `gate.explain`, on fg's loop: only the GitHub read is stubbed.
        import contextlib
        import io
        import types
        from diaktoros.cli import operations
        base = fg.Base("run")
        base.setUp()
        self.addCleanup(base.doCleanups)
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0,
                 "shadow_hold": ("", "no shadow review was queued for aaaaaaa")}
        out = io.StringIO()
        with mock.patch.object(gate, "explain_facts", return_value=facts), \
                contextlib.redirect_stdout(out):
            rc = operations.cmd_explain(types.SimpleNamespace(loop="one", pr=7))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("no shadow review was queued for aaaaaaa", out.getvalue())

    def test_explain_prints_the_note_and_nothing_without_one(self):
        import contextlib
        import io
        from diaktoros.cli import operations
        for report, want in (({"shadow": {"hold": "", "note": "no shadow review was queued"}},
                              "  shadow:     no shadow review was queued\n"),
                             ({"shadow": {"hold": "x", "note": ""}}, ""), ({}, "")):
            with self.subTest(report=report):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    operations._print_shadow_note(report, "  shadow:     ")
                self.assertEqual(out.getvalue(), want)


if __name__ == "__main__":
    unittest.main()
