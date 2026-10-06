"""Review after CI (#241): with ``review_after_ci`` on, a review waits for the head's checks.

A reviewer turn whose head still has checks running is held: no model call, no daily turn, no
retry, no notice. Its worker stays to re-queue it when its next CI read is due. After an hour from
when it was queued it reviews anyway. The setting moves from every path: the form, init, setup,
set, apply and the loop file.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_seat_models as sm  # noqa: E402
from review_loop import ci, cli, config, ledger, observer, run_supervisor  # noqa: E402
from review_loop.run_supervisor import CI_HOLD, Supervisor  # noqa: E402

RUNNING = ci.CIState(pending=["tests (3.11)"], passed=["lint"])


def raw(**extra) -> dict:
    return {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"],
            "read_token": "reader", "tokens": {},
            "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-review"},
                      "fixer": {"profile": "f", "login": "fix", "route": "w-fix"}}, **extra}


class Setting(unittest.TestCase):
    def test_off_by_default_and_only_a_boolean(self):
        self.assertIs(config.normalize(raw())["review_after_ci"], False)
        self.assertFalse(config.review_after_ci(config.normalize(raw())))
        self.assertTrue(config.review_after_ci(config.normalize(raw(review_after_ci=True))))
        for bad in ("on", 1, None):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(raw(review_after_ci=bad))

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("review_after_ci", config.apply_settings(raw(), {}))
        self.assertIs(config.apply_settings(raw(), {"review_after_ci": True})["review_after_ci"],
                      True)
        self.assertIs(config.apply_settings(raw(review_after_ci=True),
                                            {"review_after_ci": "off"})["review_after_ci"], False)


class Hold(sm.Worker):
    """The production worker, through test_seat_models' run_seat."""

    def setUp(self):
        super().setUp()
        self.ci_loop = {**self.loop, "review_after_ci": True}

    def held(self, seat="reviewer", checks=RUNNING, loop=None):
        reads = []

        def read(loop, head):
            reads.append(head)
            return checks
        with mock.patch.object(ci, "read", side_effect=read), \
             mock.patch.object(observer, "notify") as notify:
            seen, row, _ = self.run_seat(seat, loop=loop or self.ci_loop)
        return seen, row, reads, notify

    def test_running_checks_hold_the_review_and_spend_nothing(self):
        before = time.time()
        seen, row, reads, notify = self.held()
        self.assertEqual(seen, {}, "no turn launched")
        self.assertEqual(row, ("waiting", f"{CI_HOLD} on {sm.HEAD[:7]} — 1 check(s) still running or not reported"))
        self.assertEqual(reads, [sm.HEAD])
        notify.assert_not_called()                                  # no notice per poll
        with ledger.connect(self.root / "ledger.sqlite") as con:
            retries, retry_at = con.execute("SELECT retries, retry_at FROM runs").fetchone()
        self.assertEqual(retries, 0)                                # no retry spent
        self.assertGreaterEqual(retry_at, before + run_supervisor.CI_POLL_S)
        self.assertLess(retry_at, before + run_supervisor.CI_POLL_S + 60)

    def test_finished_unreadable_or_late_reviews_now(self):
        for checks in (ci.CIState(passed=["t"]), ci.CIState(failed=["t"]), None):
            with self.subTest(checks=checks):
                seen, row, _, _ = self.held(checks=checks)
                self.assertEqual(seen["role"], "reviewer")
                self.assertEqual(row[0], "succeeded")
        with mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", 0):
            seen, row, reads, _ = self.held()
        self.assertEqual((seen["role"], row[0], reads), ("reviewer", "succeeded", []))

    def test_off_running_checks_review_now_and_another_seat_never_reads_ci(self):
        seen, row, reads, _ = self.held(loop=self.loop)
        self.assertEqual((seen["role"], row[0], reads), ("reviewer", "succeeded", [sm.HEAD]))
        seen, row, reads, _ = self.held(seat="fixer", loop=self.ci_loop)
        self.assertEqual((seen["role"], row[0], reads), ("fixer", "succeeded", []))

    def test_a_cancelled_check_holds_every_review_and_says_so_once(self):
        """#363: on or off, a cancelled check holds the review (its APPROVE would be refused)
        and the one notice says to re-run it; a failed check never holds."""
        cancelled = ci.CIState(cancelled=["tests (3.11)"], passed=["lint"])
        for loop in (self.loop, self.ci_loop):
            with self.subTest(after_ci=loop.get("review_after_ci", False)):
                seen, row, _, notify = self.held(checks=cancelled, loop=loop)
                self.assertEqual(seen, {})
                self.assertEqual(row[0], "waiting")
                self.assertTrue(row[1].startswith(CI_HOLD))
                self.assertIn('1 check(s) cancelled, re-run them on GitHub ("tests (3.11)")', row[1])
                notify.assert_called_once()
                self.assertEqual(notify.call_args.args[2], "held")
                self.assertTrue(notify.call_args.kwargs["identity"].endswith(":held:ci-cancelled"))
                self.assertIn("cannot re-run CI itself", notify.call_args.kwargs["outcome"])
        seen, row, _, _ = self.held(checks=ci.CIState(failed=["t"], cancelled=["c"]))
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))

    def test_pending_wait_survives_a_failure_only_with_review_after_ci(self):
        """Pinned choice: with review_after_ci, a failed check beside a running one still
        holds (one review names every failure); a failure releases cancelled/missing holds."""
        mixed = ci.CIState(failed=["lint"], pending=["tests (3.11)"])
        seen, row, _, _ = self.held(checks=mixed)
        self.assertEqual(seen, {})
        self.assertEqual(row[0], "waiting")
        self.assertTrue(row[1].startswith(CI_HOLD))
        seen, row, _, _ = self.held(checks=mixed, loop=self.loop)   # setting off: reviews now
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))
        both = ci.CIState(failed=["lint"], cancelled=["c"], pending=["p"])
        seen, row, _, _ = self.held(checks=both, loop=self.loop)
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))


class ConflictHold(Hold):
    """A PR that conflicts with its base gets no CI from GitHub: hold, spend nothing."""

    def dirty(self, state="dirty", sha=sm.HEAD, checks=ci.CIState(passed=["t"]), **extra):
        pr = {"number": 7, "state": "open", "draft": False, "head": {"sha": sha, "ref": "fix-7"},
              "base": {"ref": "main"}, "mergeable_state": state, **extra}
        reads = []

        def read(loop, head):
            reads.append(head)
            return checks
        with mock.patch.object(ci, "read", side_effect=read), \
             mock.patch.object(observer, "notify"):
            seen, row, _ = self.run_seat("reviewer", loop=self.loop, pr=pr)
        return seen, row, reads

    def test_a_dirty_head_is_held_with_no_model_call(self):
        seen, row, reads = self.dirty()
        self.assertEqual(seen, {})
        self.assertEqual(row, ("waiting", f"{CI_HOLD} on {sm.HEAD[:7]} — conflicts with main "
                                          "— GitHub runs no CI on it"))
        with ledger.connect(self.root / "ledger.sqlite") as con:
            self.assertEqual(con.execute("SELECT retries FROM runs").fetchone()[0], 0)

    def test_clean_or_unknown_starts_normally(self):
        for state in ("clean", "unstable", "unknown", None):
            with self.subTest(state=state):
                seen, row, _ = self.dirty(state=state)
                self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))

    def test_a_merge_push_supersedes_the_held_row(self):
        seen, row, _ = self.dirty(sha="b" * 40)
        self.assertEqual(seen, {})
        self.assertNotEqual(row[0], "succeeded")
        self.assertNotIn("conflicts with", str(row[1]))

    def test_a_closed_or_draft_dirty_pr_is_not_held_for_conflicts(self):
        for extra in ({"state": "closed"}, {"draft": True}):
            with self.subTest(extra=extra):
                # Eligibility (closed/draft) is the claim stage's job, not this hold's.
                seen, row, _ = self.dirty(**extra)
                self.assertNotIn("conflicts with", str(row[1]))
                self.assertNotEqual(row[0], "waiting")

    def test_the_wait_cap_still_reviews(self):
        with mock.patch.object(run_supervisor, "CI_WAIT_MAX_S", 0):
            seen, row, _ = self.dirty(checks=ci.CIState(missing=["tests"]))
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))


class Linger(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name)
        self.sup = Supervisor(self.tmp / "runs.sqlite")
        self.sup.enqueue("d", "acme/widgets", 7, "a" * 40, "reviewer")
        self.sup.recover = mock.Mock()

    def hold(self, retry_at, error=f"{CI_HOLD} on aaaaaaa — 1 check(s) still running"):
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=?", (retry_at, error))

    def test_only_a_worker_that_held_for_ci_lingers(self):
        self.hold(0)
        self.sup.linger_for_ci(sleep=self.fail)
        self.sup.recover.assert_not_called()

    def test_sleeps_until_due_then_schedules_it(self):
        now = time.time()
        self.hold(now + 5)
        self.sup.held_for_ci = True
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            self.hold(0)                            # time passes: the read is now due
        self.sup.linger_for_ci(sleep=sleep)
        self.assertEqual(len(slept), 1)
        self.assertLessEqual(slept[0], 7)
        self.sup.recover.assert_called_once()

    def test_another_hold_is_not_ours_to_wait_for(self):
        self.hold(0, error="held: reviewer daily turn cap (10) reached — resumes 00:00")
        self.sup.held_for_ci = True
        self.sup.linger_for_ci(sleep=self.fail)
        self.sup.recover.assert_not_called()


LOOP_ID = "aftercheck"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)   # one loop per repo
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None):
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None):
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> bool:
        return json.loads(LOOP_FILE.read_text()).get("review_after_ci", False)

    def test_init_starts_from_the_form_and_the_flag_wins(self):
        for extra, settings, want in (((), None, False), ((), {"review_after_ci": True}, True),
                                      (("--review-after-ci", "off"), {"review_after_ci": True},
                                       False),
                                      (("--review-after-ci", "on"), None, True)):
            with self.subTest(extra=extra, settings=settings):
                LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertIs(self.written(), want)

    def test_set_and_apply_move_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-after-ci", "on")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), True)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"review_after_ci": False})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertIs(self.written(), False)

    def test_setup_hands_init_the_answer(self):
        args = t.parser_for({"review_after_ci": True}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--review-after-ci=on", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--review-after-ci", "on"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--review-after-ci=on", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--review-after-ci=off", argv)


# Hold inherits test_seat_models' Worker for its run_seat; its own tests run in that module.
for _name in [n for n in dir(sm.Worker) if n.startswith("test_")]:
    setattr(Hold, _name, None)

if __name__ == "__main__":
    unittest.main()
