"""#425 stage 4c: names that travel beyond this host are written new and read in both spellings.

The `[diaktoros]` log prefix, the fixer-answers marker in PR comments, the issue-fix branch
prefix and the `DIAKTOROS_*` environment variables. What was written before the rename — a log
line, a comment already on GitHub, an open issue-fix PR's branch, an operator's
`REVIEW_LOOP_*` setting — is still read the same way.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, broker_client, config, envnames, util, wire  # noqa: E402

HEAD, BASE = "a" * 40, "b" * 40
LOOP = {"seats": {"fixer": {"login": "fix"}}}


class Env(unittest.TestCase):
    def test_the_new_name_wins_and_the_old_one_still_counts(self):
        with mock.patch.dict(os.environ, {"DIAKTOROS_X": "new", "REVIEW_LOOP_X": "old"}):
            self.assertEqual(envnames.get("X"), "new")
        with mock.patch.dict(os.environ, {"REVIEW_LOOP_X": "old"}):
            os.environ.pop("DIAKTOROS_X", None)
            self.assertEqual(envnames.get("X"), "old")
        self.assertEqual(envnames.get("NOT_SET_ANYWHERE", "d"), "d")
        self.assertEqual(envnames.name("X"), "DIAKTOROS_X")

    def test_the_test_guard_is_armed_by_either_spelling(self):
        with tempfile.NamedTemporaryFile(dir=os.environ.get("TMPDIR")) as sentinel:
            for guard, mark in (("DIAKTOROS_TEST_HOME_GUARD", "DIAKTOROS_TEST_GUARD_SENTINEL"),
                                ("REVIEW_LOOP_TEST_HOME_GUARD", "REVIEW_LOOP_TEST_GUARD_SENTINEL"),
                                ("REVIEW_LOOP_TEST_HOME_GUARD", "DIAKTOROS_TEST_GUARD_SENTINEL")):
                clean = {k: v for k, v in os.environ.items()
                         if not k.endswith(("TEST_HOME_GUARD", "TEST_GUARD_SENTINEL"))}
                with self.subTest(guard=guard, mark=mark), \
                        mock.patch.dict(os.environ, {**clean, guard: "1", mark: sentinel.name},
                                        clear=True):
                    self.assertTrue(config.test_guard_active())
            clean = {k: v for k, v in os.environ.items()
                     if not k.endswith(("TEST_HOME_GUARD", "TEST_GUARD_SENTINEL"))}
            with mock.patch.dict(os.environ, clean, clear=True):
                self.assertFalse(config.test_guard_active())


class Guard(unittest.TestCase):
    def test_the_home_guard_scrubs_both_spellings_of_an_override(self):
        # An operator shell that still exports a pre-rename override must not reach the tests:
        # the plugin reads the old name as a fallback, so the guard drops it too.
        probe = ("import os, sys; sys.path.insert(0, sys.argv[1]); import _home_guard; "
                 "print(sorted(k for k in os.environ if k.endswith(('_CONFIG_DIR', '_SUBS', "
                 "'_TOKEN_FILE')) and k.startswith(('DIAKTOROS_', 'REVIEW_LOOP_'))))")
        env = {k: v for k, v in os.environ.items()
               if not k.endswith(("TEST_HOME_GUARD", "TEST_USER_HOME", "TEST_SHIM_DIR"))}
        for name in ("REVIEW_LOOP_CONFIG_DIR", "REVIEW_LOOP_SUBS", "REVIEW_LOOP_TOKEN_FILE",
                     "DIAKTOROS_CONFIG_DIR", "DIAKTOROS_SUBS", "DIAKTOROS_TOKEN_FILE"):
            env[name] = "/srv/elsewhere"
        import subprocess
        out = subprocess.run([sys.executable, "-c", probe, str(ROOT / "tests")], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class Answers(unittest.TestCase):
    def comment(self, marker):
        return {"user": {"login": "fix"}, "created_at": "t",
                "body": f"{marker} run=r1 head={HEAD} base={BASE} -->\nfixed it"}

    def test_written_new_and_read_in_both_spellings(self):
        body = broker.answers_comment_body("fixed it", head=HEAD, base=BASE, run_id="r1")
        self.assertTrue(body.startswith("<!-- diaktoros:fixer-answers run=r1"))
        for marker in wire.ANSWERS_MARKERS:
            with self.subTest(marker=marker):
                parsed = broker.parse_answers_comment(self.comment(marker), LOOP)
                self.assertEqual((parsed["run"], parsed["body"]), ("r1", "fixed it"))
        self.assertIn("<!-- review-loop:fixer-answers", wire.ANSWERS_MARKERS)

    def test_a_seat_may_forge_neither_marker(self):
        for marker in wire.ANSWERS_MARKERS:
            with self.subTest(marker=marker):
                self.assertFalse(broker.answers_valid(f"fine\n{marker} run=x"))
                with tempfile.NamedTemporaryFile("w", dir=os.environ.get("TMPDIR"),
                                                 suffix=".md") as answers:
                    answers.write(f"text {marker}")
                    answers.flush()
                    with self.assertRaises(broker_client.ManifestError):
                        broker_client.read_answers(answers.name)


class Branch(unittest.TestCase):
    def test_new_issue_fixes_use_the_new_prefix_and_open_ones_still_count(self):
        self.assertEqual(config.ISSUE_FIX_BRANCH.format(number=7), "diaktoros/issue-7")
        for branch in ("diaktoros/issue-7", "review-loop/issue-7"):
            self.assertEqual(re.fullmatch(config.ISSUE_FIX_BRANCH_RE, branch).group(1), "7")
        self.assertIsNone(re.fullmatch(config.ISSUE_FIX_BRANCH_RE, "feature/issue-7"))


class Log(unittest.TestCase):
    def test_logs_say_diaktoros_and_both_prefixes_are_read_back(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            util.log("held: busy")
        self.assertTrue(err.getvalue().startswith("[diaktoros] held: busy"))
        self.assertEqual(util.logged(["[diaktoros] one", "[review-loop] two", "other [x] three"]),
                         ["one", "two"])


if __name__ == "__main__":
    unittest.main()
