"""#1784: a red main that looks like a flake is rerun once, as a maintainer, before any notice.

``main_check.sweep`` with GitHub mocked: a failing test that passed in a sibling shard (or a
timeout) is rerun and no ``main_red`` is sent; the rerun's green sends one routine ``main_flaked``;
a second red sends ``main_red`` saying a rerun was tried. Anything else is a ``main_red`` as before.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main_check import COMPARE, FakeState  # noqa: E402
from diaktoros import ci, ci_tests, doctor, gh, main_check, main_flake, observer  # noqa: E402
from diaktoros.doctor import identity_checks  # noqa: E402

HEAD = "b" * 40
SHARDS = ["unit (3.11, 3/3)", "unit (3.14, 3/3)"]
LOOP = {"id": "w", "repo": "acme/widgets", "base": "main", "read_token": "reader",
        "required_checks": SHARDS, "main_flake_rerun": True,
        "triage": {"maintainers": ["Fix-Bot", "Boss"]},
        "tokens": {"fix-bot": "/t/fix", "boss": "/t/boss", "reader": "/t/read"},
        "seats": {"fixer": {"login": "fix-bot"}, "reviewer": {"login": "review-bot"}}}
TEST = "tests.test_probe.Probe.test_rust_namespace"
FAILED_LOG = (f"2026-11-03T01:00:00Z test_rust_namespace ({TEST}) ... ERROR\n"
              "2026-11-03T01:00:01Z ======================================================================\n"
              f"2026-11-03T01:00:01Z ERROR: test_rust_namespace ({TEST})\n"
              "2026-11-03T01:00:01Z ----------------------------------------------------------------------\n"
              "2026-11-03T01:00:01Z AssertionError: probe said no\n"
              "2026-11-03T01:00:02Z Ran 400 tests in 300s\n")
TIMEOUT_LOG = FAILED_LOG.replace("AssertionError: probe said no",
                                 "subprocess.TimeoutExpired: Command 'probe' timed out after 60 seconds")


def red(ids=(101,)):
    return ci.CIState(failed=[SHARDS[0]], passed=[SHARDS[1]], ids=dict(zip(SHARDS, (*ids, 102))),
                      urls={SHARDS[0]: "https://x/run/1"})


GREEN = ci.CIState(passed=list(SHARDS), ids={SHARDS[0]: 201, SHARDS[1]: 102})


class Flake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = FakeState(self.tmp.name)
        self.notices, self.posts = [], []

    def sweep(self, state, log=FAILED_LOG, sibling="ok", loop=LOOP, refused=""):
        def api(_loop, path, method="GET", login=None, **_kw):
            if method == "POST":
                self.posts.append((path, login))
                return None
            return {"sha": HEAD} if "/commits/" in path else COMPARE
        job = (77, "failure", log)
        with mock.patch.object(gh, "api", side_effect=api), \
                mock.patch.object(gh, "last_error", return_value=refused), \
                mock.patch.object(ci, "read", return_value=state), \
                mock.patch.object(main_flake, "_failed_job", return_value=job), \
                mock.patch.object(ci_tests, "read", return_value={TEST: sibling} if sibling else {}), \
                mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **kw: self.notices.append((a, kw))):
            return main_check.sweep(loop, self.st)

    def memory(self):
        return self.st.kv_get(self.st.dir / main_check.FILE, {})

    def test_a_test_green_in_a_sibling_shard_is_rerun_once_and_its_green_is_a_main_flaked(self):
        self.assertFalse(self.sweep(red()))
        self.assertEqual(self.notices, [])                       # no main_red for a flake
        self.assertEqual(self.posts, [("/repos/acme/widgets/actions/runs/77/rerun-failed-jobs", "boss")])
        tried = self.memory()["rerun"]
        self.assertEqual((tried["head"], tried["tests"], tried["ids"]), (HEAD, [TEST], [101]))
        # The old failed job read again before the rerun shows: still waiting, nothing sent.
        self.assertFalse(self.sweep(red()))
        self.assertEqual((self.notices, len(self.posts)), ([], 1))
        self.assertFalse(self.sweep(GREEN))
        (args, kw), = self.notices
        self.assertEqual((args[2], args[4]), ("main_flaked", HEAD))
        self.assertFalse(observer.is_urgent("main_flaked"))     # routine: subscribable, labelled
        for table in (observer.EVENTS, observer.LABEL, observer.EMOJI):
            self.assertIn("main_flaked", table)
        for text in (TEST, "passed in a sibling job", "boss", "No PR is blamed"):
            self.assertIn(text, kw["outcome"])
        self.assertFalse(self.sweep(GREEN))                      # green is judged once
        self.assertEqual(len(self.notices), 1)

    def test_a_rerun_that_fails_again_is_a_main_red_saying_so_and_is_not_rerun_twice(self):
        self.sweep(red())
        self.assertTrue(self.sweep(red(ids=(301,))))             # the rerun's own job, red
        (args, kw), = self.notices
        self.assertEqual(args[2], "main_red")
        self.assertIn("A rerun of the failed jobs was tried, and it failed again.", kw["outcome"])
        self.assertEqual(len(self.posts), 1)

    def test_a_rerun_that_never_shows_is_red_after_the_wait(self):
        self.sweep(red())
        tried = {**self.memory()["rerun"], "at": 0}
        main_check._remember(self.st, self.st.dir / main_check.FILE, "rerun", tried)
        self.assertTrue(self.sweep(red()))
        self.assertIn("tried, and it failed again", self.notices[0][1]["outcome"])

    def test_a_timeout_is_a_flake_even_with_no_sibling_run(self):
        self.assertFalse(self.sweep(red(), log=TIMEOUT_LOG, sibling=None))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.memory()["rerun"]["why"], "timed out")

    def test_a_real_failure_is_a_main_red_as_before_with_no_rerun(self):
        for sibling in ("FAIL", None):                           # red there too, or never ran there
            with self.subTest(sibling=sibling):
                self.setUp()
                self.assertTrue(self.sweep(red(), sibling=sibling))
                self.assertEqual(self.posts, [])
                self.assertEqual(self.notices[0][0][2], "main_red")
                self.assertNotIn("rerun", self.notices[0][1]["outcome"])

    def test_an_unreadable_job_is_no_flake(self):
        with mock.patch.object(main_flake, "_failed_job", return_value=None):
            self.assertIsNone(main_flake.flake(LOOP, red(), HEAD))

    def test_off_never_reruns(self):
        self.assertTrue(self.sweep(red(), loop={**LOOP, "main_flake_rerun": False}))
        self.assertEqual(self.posts, [])

    def test_no_maintainer_token_says_so_and_a_refused_rerun_says_why(self):
        alone = {**LOOP, "triage": {"maintainers": ["fix-bot"]}}        # the only one is a seat
        self.assertTrue(self.sweep(red(), loop=alone))
        self.assertIn("no rerun was tried", self.notices[0][1]["outcome"])
        self.assertEqual(self.posts, [])
        self.setUp()
        self.assertTrue(self.sweep(red(), refused="HTTP 403 Resource not accessible"))
        self.assertIn("the rerun of run 77 failed: HTTP 403", self.notices[0][1]["outcome"])
        self.assertNotIn("rerun", self.memory())

    def test_the_rerun_login_is_a_maintainer_with_a_token_and_never_a_seat(self):
        self.assertEqual(main_flake.rerun_login(LOOP), "boss")
        self.assertEqual(main_flake.rerun_login({**LOOP, "tokens": {"fix-bot": "/t"}}), "")
        self.assertEqual(main_flake.rerun_login({**LOOP, "triage": {}}), "")

    def test_timeouts_needs_every_failure_to_be_one(self):
        self.assertTrue(main_flake.timeouts(TIMEOUT_LOG))
        self.assertFalse(main_flake.timeouts(FAILED_LOG))
        both = TIMEOUT_LOG.replace("Ran 400", "======================================================================\n"
                                   f"FAIL: test_other ({TEST}x)\nAssertionError: real\nRan 400")
        self.assertFalse(main_flake.timeouts(both))
        self.assertFalse(main_flake.timeouts("no failures here"))


class Doctor(unittest.TestCase):
    def check(self, loop, push=True, offline=False):
        repo = {"permissions": {"push": push}}
        with mock.patch.object(gh, "api", return_value=repo):
            return identity_checks.check_main_flake_rerun(loop, offline)

    def test_each_state(self):
        self.assertIsNone(self.check({**LOOP, "main_flake_rerun": False}))
        self.assertEqual(self.check(LOOP).status, doctor.VERIFIED)
        self.assertIn("boss", self.check(LOOP).detail)
        for loop, push in (({**LOOP, "triage": {"maintainers": ["fix-bot"]}}, True),
                           ({**LOOP, "required_checks": []}, True), (LOOP, False)):
            self.assertEqual(self.check(loop, push).status, doctor.MISMATCH)
        self.assertEqual(self.check(LOOP, offline=True).status, doctor.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
