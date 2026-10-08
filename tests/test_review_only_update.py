"""A review-only PR that conflicts after a merge (#412): a notice that says what to do, and the
opt-in host merge (``review_only_update``).

Real Git, a local bare remote. ``main`` moves twice (a squash-merged "(#428)" and a "(#430)");
the PR branch ``feat`` changes ``a.py``. Nothing here reaches the network.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import broker, config, review_only_conflict, safe_push  # noqa: E402

REPO = "acme/widgets"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
       "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}


def git(*args):
    return subprocess.check_output(["git", *args], env=ENV, text=True).strip()


class Base(unittest.TestCase):
    CONFLICT = True
    WORKFLOW = False

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.src = src = self.root / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", str(src))
        (src / "a.py").write_text("x = 1\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "base")
        git("-C", str(src), "checkout", "-qb", "feat")
        (src / "a.py").write_text("x = 2\n")
        git("-C", str(src), "commit", "-qam", "the PR")
        self.head = git("-C", str(src), "rev-parse", "HEAD")
        git("-C", str(src), "checkout", "-q", "main")
        (src / "a.py" if self.CONFLICT else src / "c.py").write_text("x = 3\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "Change a thing (#428)")
        (src / "d.py").write_text("d\n")
        if self.WORKFLOW:
            (src / ".github" / "workflows").mkdir(parents=True)
            (src / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "Another (#430)")
        self.remote = str(self.root / "remote.git")
        git("init", "-q", "--bare", self.remote)
        git("-C", str(src), "push", "-q", self.remote, "main", "feat")
        token = self.root / "token"
        token.write_text("not-a-real-token")
        self.loop = {"id": "w", "repo": REPO, "base": "main", "read_token": "read",
                     "tokens": {"fix": str(token), "read": str(token)},
                     "seats": {"fixer": {"login": "fix"}, "reviewer": {"login": "rev"}},
                     "review_only": ["owner"], "review_only_update": True,
                     "unattended_fixer_push": True, "attribution": False,
                     "state_dir": str(self.root / "state")}
        (self.root / "state").mkdir()
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/does/not/exist"})
        env.start()
        self.addCleanup(env.stop)
        self.live = {"number": 7, "state": "open", "draft": False, "user": {"login": "owner"},
                     "base": {"ref": "main", "repo": {"full_name": REPO}},
                     "head": {"sha": self.head, "ref": "feat", "repo": {"full_name": REPO}}}

    def api(self, loop, path, method="GET", body=None, login=None):
        if path == "/user":
            return {"login": login, "id": 5}
        if path.endswith("/pulls/7"):
            return self.live
        if "/git/ref/heads/" in path:
            return {"ref": "refs/heads/feat",
                    "object": {"sha": git("--git-dir", self.remote, "rev-parse", "feat")}}
        return {}

    def update(self, **kw):
        with mock.patch.object(safe_push.gh, "api", side_effect=self.api), \
             mock.patch.object(safe_push, "_isolated", wraps=safe_push._isolated) as iso:
            iso.side_effect = lambda loop, login, ident, remote: safe_push.__dict__[
                "_isolated_orig"](loop, login, ident, self.remote)
            return safe_push.update_branch(self.loop, repo=REPO, number=7, head=self.head,
                                           branch="feat", **kw)


# `_isolated` takes the remote through a seam; keep the real one reachable for the wrapper.
safe_push._isolated_orig = safe_push._isolated


class Notice(Base):
    def facts(self):
        return review_only_conflict.assess(self.loop, 7, self.head, self.live, remote=self.remote)

    def test_names_the_merged_prs_the_file_and_the_commands(self):
        facts = self.facts()
        self.assertEqual(facts["merged"], [428, 430])
        self.assertEqual(facts["conflicted"], ["a.py"])
        text = review_only_conflict.message(self.loop, "owner", facts)
        self.assertIn("#428, #430", text)
        self.assertIn("the conflict is in one file: a.py", text)
        self.assertIn("git fetch origin && git switch feat && git merge origin/main", text)

    def test_a_dry_merge_pushes_nothing(self):
        before = git("--git-dir", self.remote, "rev-parse", "feat")
        self.facts()
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "feat"), before)

    def test_a_fork_is_notice_only_and_never_read(self):
        self.live["head"]["repo"] = {"full_name": "someone/widgets"}
        facts = review_only_conflict.assess(self.loop, 7, self.head, self.live,
                                            remote=self.remote)
        self.assertTrue(facts["fork"])
        pushed, why = review_only_conflict.update(self.loop, 7, self.head, facts)
        self.assertEqual((pushed, why), ("", "a fork branch is notice-only"))


class UpdateConflicting(Base):
    def test_a_real_conflict_is_never_pushed(self):
        before = git("--git-dir", self.remote, "rev-parse", "feat")
        with self.assertRaises(safe_push.NotClean) as caught:
            self.update()
        self.assertIn("a.py", str(caught.exception))
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "feat"), before)

    def test_the_update_refuses_when_the_setting_is_off(self):
        self.loop["review_only_update"] = False
        with self.assertRaises(broker.BrokerDenied):
            self.update()
        facts = {"fork": False, "conflicted": [], "workflows": False, "branch": "feat"}
        self.assertEqual(review_only_conflict.update(self.loop, 7, self.head, facts), ("", ""))


class UpdateClean(Base):
    CONFLICT = False

    def test_a_clean_merge_is_pushed_once_with_two_parents(self):
        done = self.update()
        remote_head = git("--git-dir", self.remote, "rev-parse", "feat")
        self.assertEqual((done["outcome"], done["new_head"]), ("published", remote_head))
        parents = git("--git-dir", self.remote, "rev-list", "--parents", "-n1", remote_head).split()
        self.assertEqual(parents[1], self.head)
        self.assertEqual(parents[2], git("--git-dir", self.remote, "rev-parse", "main"))

    def test_an_author_push_in_between_fails_the_lease_and_overwrites_nothing(self):
        theirs = {}

        def author_pushes(*a, **kw):
            if "sha" not in theirs:
                src = self.src
                git("-C", str(src), "checkout", "-q", "feat")
                (src / "mine.py").write_text("mine\n")
                git("-C", str(src), "add", "-A")
                git("-C", str(src), "commit", "-qm", "author push")
                git("-C", str(src), "push", "-q", self.remote, "feat")
                theirs["sha"] = git("-C", str(src), "rev-parse", "HEAD")
            return {"login": "fix", "id": 5}

        real = self.api
        calls = {"n": 0}

        def api(loop, path, method="GET", body=None, login=None):
            if path.endswith("/pulls/7"):
                calls["n"] += 1
                if calls["n"] == 2:      # between the dry merge and the push
                    author_pushes()
            return real(loop, path, method, body, login)

        self.api = api
        with self.assertRaises(safe_push.PushFailure):
            self.update()
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "feat"), theirs["sha"])

    def test_with_the_update_off_nothing_is_pushed_by_the_watchdog_path(self):
        self.loop["review_only_update"] = False
        facts = review_only_conflict.assess(self.loop, 7, self.head, self.live, remote=self.remote)
        self.assertEqual(review_only_conflict.update(self.loop, 7, self.head, facts), ("", ""))
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "feat"), self.head)


class UpdateWorkflow(Base):
    CONFLICT = False
    WORKFLOW = True

    def test_a_workflow_change_is_notice_only(self):
        with self.assertRaises(safe_push.NotClean):
            self.update()
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "feat"), self.head)


class Setting(unittest.TestCase):
    RAW = {"repo": REPO, "fixers": ["fix"], "reviewers": ["rev"], "read_token": "reader",
           "tokens": {}, "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-r"},
                                   "fixer": {"profile": "f", "login": "fix", "route": "w-f"}}}

    def test_off_by_default_strict_bool_and_on_the_form(self):
        self.assertIs(config.normalize(dict(self.RAW))["review_only_update"], False)
        self.assertFalse(config.review_only_update(config.normalize(dict(self.RAW))))
        with self.assertRaises(config.ConfigError):
            config.normalize({**self.RAW, "review_only_update": "yes"})
        self.assertNotIn("review_only_update", config.apply_settings(dict(self.RAW), {}))
        got = config.apply_settings(dict(self.RAW), {"review_only_update": True})
        self.assertIs(got["review_only_update"], True)
        self.assertIn("review_only_update", config.SETTINGS_SCHEMA)
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn("  review_only_update:", text)


import json  # noqa: E402
import run_tests as t  # noqa: E402

LOOP_ID = "roupdate"
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
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)
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

    def written(self):
        return json.loads(LOOP_FILE.read_text()).get("review_only_update")

    def test_off_by_default_and_every_path_needs_the_acknowledgement(self):
        self.assertEqual(self.init()[0], 0)
        self.assertIs(self.written(), False)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-update", "on")
        self.assertEqual(rc, 2, out)                      # refused without the acknowledgement
        self.assertIs(self.written(), False)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-update", "on",
                           "--acknowledge-branch-push")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), True)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-update", "off")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), False)

    def test_init_refuses_it_without_the_acknowledgement(self):
        rc, out = self.init("--review-only-update", "on")
        self.assertEqual(rc, 2, out)
        self.assertFalse(LOOP_FILE.exists())
        rc, out = self.init("--review-only-update", "on", "--acknowledge-branch-push")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), True)


if __name__ == "__main__":
    unittest.main()
