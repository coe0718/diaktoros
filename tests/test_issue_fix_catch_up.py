"""#2591: an issue fix whose base moved during the turn opens up to date.

The fix commit is built on the base the turn started from; with ``update_behind`` on, the host
merges the base's tip into it before the push when that merge is clean and brings no workflow
change, so the PR opens on a head that is not behind. Real local bare Git, no network.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, config, safe_push  # noqa: E402

REPO = "acme/widgets"
BRANCH = "diaktoros/issue-12"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.org",
       "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.org"}


class CatchUp(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.remote, self.local = str(root / "remote.git"), str(root / "creator.git")
        self.git("init", "--bare", self.remote)
        self.git("init", "--bare", self.local)
        self.base = self.commit({"a.txt": "a\n", "b.txt": "b\n", ".github/workflows/ci.yml": "on: push\n"})
        self.git("--git-dir", self.local, "push", self.remote, f"{self.base}:refs/heads/main")
        self.loop = {"tokens": {"fix": str(root / "dummy")}, "base": "main"}
        Path(self.loop["tokens"]["fix"]).write_text("not-a-real-token")
        for patch in (mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1",
                                                   "GIT_CONFIG_KEY_0": "protocol.file.allow",
                                                   "GIT_CONFIG_VALUE_0": "never",
                                                   "GIT_CONFIG_GLOBAL": "/does/not/exist"}),):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, *args, **kwargs):
        return subprocess.check_output(["git", *args], text=True, **{"env": ENV, "stderr": subprocess.DEVNULL, **kwargs}).strip()

    def commit(self, files: dict, parent: str | None = None) -> str:
        """A commit of ``files`` (the whole tree) in the creator repository."""
        entries = []
        for path, text in files.items():
            blob = self.git("--git-dir", self.local, "hash-object", "-w", "--stdin", input=text)
            entries.append(f"100644 blob {blob}\t{path}")
        index = {"GIT_INDEX_FILE": str(Path(self.local) / "fixture.index")}
        self.git("--git-dir", self.local, "read-tree", "--empty", env={**ENV, **index})
        for entry in entries:
            meta, path = entry.split("\t")
            mode, _, blob = meta.split()
            self.git("--git-dir", self.local, "update-index", "--add", "--cacheinfo",
                     f"{mode},{blob},{path}", env={**ENV, **index})
        tree = self.git("--git-dir", self.local, "write-tree", env={**ENV, **index})
        return self.git("--git-dir", self.local, "commit-tree", tree,
                        *(["-p", parent] if parent else []), input="commit\n")

    def move_main(self, changes: dict) -> str:
        """The base moves on while the turn runs: one commit after the turn's base, changing
        ``changes`` (forced, so each case starts from the same base)."""
        files = {"a.txt": "a\n", "b.txt": "b\n", ".github/workflows/ci.yml": "on: push\n", **changes}
        tip = self.commit(files, self.base)
        self.git("--git-dir", self.local, "push", self.remote, f"+{tip}:refs/heads/main")
        return tip

    def push(self, catch_up: bool):
        journal = []
        head = safe_push._git_cas(self.loop, REPO, BRANCH, self.base, [("a.txt", b"fixed\n")],
                                  "Fix the typo", "fix", {"name": "fix", "email": "fix@example.org"},
                                  before_push=journal.append, remote=self.remote,
                                  from_branch="main", catch_up=catch_up)
        self.assertEqual(journal, [head])        # what is audited is what is pushed
        self.assertEqual(self.git("--git-dir", self.remote, "rev-parse", f"refs/heads/{BRANCH}"), head)
        return head

    def parents(self, sha: str) -> list[str]:
        return self.git("--git-dir", self.remote, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    def test_a_base_that_moved_is_merged_in_before_the_branch_opens(self):
        tip = self.move_main({"b.txt": "b moved\n"})
        head = self.push(catch_up=True)
        fix, merged_tip = self.parents(head)
        self.assertEqual((self.parents(fix), merged_tip), ([self.base], tip))
        self.assertEqual(self.git("--git-dir", self.remote, "show", f"{head}:a.txt"), "fixed")
        self.assertEqual(self.git("--git-dir", self.remote, "show", f"{head}:b.txt"), "b moved")
        self.assertEqual(self.git("--git-dir", self.remote, "log", "-1", "--format=%s", head),
                         f"Merge main into {BRANCH}")
        self.assertEqual(self.git("--git-dir", self.remote, "merge-base", "--is-ancestor", tip, head), "")

    def test_the_fix_alone_is_pushed_when_the_merge_would_not_be_clean_or_safe(self):
        # A conflict, or workflow changes a push without the `workflow` scope cannot carry: the
        # branch opens as before, and review and update_behind take it from there.
        for case, change in (("conflict", {"a.txt": "a moved\n"}),
                             ("workflow change", {".github/workflows/ci.yml": "on: [push]\n"})):
            with self.subTest(case=case):
                self.move_main(change)
                head = self.push(catch_up=True)
                self.assertEqual(self.parents(head), [self.base])
                self.git("--git-dir", self.remote, "update-ref", "-d", f"refs/heads/{BRANCH}")

    def test_nothing_is_merged_when_the_base_did_not_move_or_catch_up_is_off(self):
        self.assertEqual(self.parents(self.push(catch_up=True)), [self.base])
        self.git("--git-dir", self.remote, "update-ref", "-d", f"refs/heads/{BRANCH}")
        self.move_main({"b.txt": "b moved\n"})
        self.assertEqual(self.parents(self.push(catch_up=False)), [self.base])


class OpenBranchAsksForTheCatchUp(unittest.TestCase):
    """open_branch asks _git_cas to catch up exactly when ``update_behind`` is on."""

    def test_update_behind_decides(self):
        import base64
        import hashlib
        data = b"fixed\n"
        manifest = {"base_head": "a" * 40, "message": "Fix the typo",
                    "files": [{"path": "README.md", "content_b64": base64.b64encode(data).decode(),
                               "sha256": hashlib.sha256(data).hexdigest()}]}
        for setting in (True, False):
            with self.subTest(update_behind=setting):
                loop = {"repo": REPO, "base": "main", "update_behind": setting}
                cas = mock.Mock(side_effect=broker.BrokerDenied("stop here"))
                with mock.patch.object(config, "issue_fixes_enabled", return_value=True), \
                        mock.patch.object(broker, "authorize_issue_fix", return_value="fix"), \
                        mock.patch.object(safe_push, "_fixer_identity", return_value={}), \
                        mock.patch.object(safe_push, "_absent_branch"), \
                        mock.patch.object(safe_push, "_git_cas", cas), \
                        mock.patch.object(safe_push, "_confirm_ref", return_value=None), \
                        self.assertRaises(broker.BrokerDenied):
                    safe_push.open_branch(loop, repo=REPO, number=12, base="a" * 40,
                                          branch=BRANCH, manifest=manifest)
                self.assertIs(cas.call_args.kwargs["catch_up"], setting)


if __name__ == "__main__":
    unittest.main()
