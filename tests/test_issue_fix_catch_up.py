"""#2591: an issue fix whose base moved during the turn opens up to date.

The fix commit is built on the base the turn started from; when the base moved meanwhile, the
host merges the base's tip into it before the push, on every loop (``update_behind`` or not, #2606),
when that merge is clean and brings no workflow change, so the PR opens on a head that is not
behind. Real local bare Git, no network.
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


class Fixture(unittest.TestCase):
    """A base branch on a local bare remote, and the issue fix's push to it."""

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

    def push(self):
        journal = []
        head = safe_push._git_cas(self.loop, REPO, BRANCH, self.base, [("a.txt", b"fixed\n")],
                                  "Fix the typo", "fix", {"name": "fix", "email": "fix@example.org"},
                                  before_push=journal.append, remote=self.remote, from_branch="main")
        self.assertEqual(journal, [head])        # what is audited is what is pushed
        self.assertEqual(self.git("--git-dir", self.remote, "rev-parse", f"refs/heads/{BRANCH}"), head)
        return head

    def parents(self, sha: str) -> list[str]:
        return self.git("--git-dir", self.remote, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    def forget_branch(self):
        self.git("--git-dir", self.remote, "update-ref", "-d", f"refs/heads/{BRANCH}")


class CatchUp(Fixture):
    def test_a_base_that_moved_is_merged_in_before_the_branch_opens(self):
        # Every loop: the fixture loop never set update_behind (maintainer decision on #2591).
        self.assertFalse(config.update_behind(self.loop))
        tip = self.move_main({"b.txt": "b moved\n"})
        head = self.push()
        fix, merged_tip = self.parents(head)
        self.assertEqual((self.parents(fix), merged_tip), ([self.base], tip))
        self.assertEqual(self.git("--git-dir", self.remote, "show", f"{head}:a.txt"), "fixed")
        self.assertEqual(self.git("--git-dir", self.remote, "show", f"{head}:b.txt"), "b moved")
        self.assertEqual(self.git("--git-dir", self.remote, "log", "-1", "--format=%s", head),
                         f"Merge main into {BRANCH}")
        self.assertEqual(self.git("--git-dir", self.remote, "merge-base", "--is-ancestor", tip, head), "")

    def test_the_fix_alone_is_pushed_when_the_merge_would_not_be_clean_or_safe(self):
        # A conflict, or workflow changes a push without the `workflow` scope cannot carry: the
        # branch opens as before, behind its base (#2606).
        for case, change in (("conflict", {"a.txt": "a moved\n"}),
                             ("workflow change", {".github/workflows/ci.yml": "on: [push]\n"})):
            with self.subTest(case=case):
                self.move_main(change)
                self.assertEqual(self.parents(self.push()), [self.base])
                self.forget_branch()

    def test_the_fix_alone_is_pushed_when_the_merge_cannot_be_computed(self):
        # The merge is optional: _dry's own refusal (merge-tree failed or said nothing usable)
        # leaves the valid fix to open on its own instead of stranding it with no PR.
        self.move_main({"b.txt": "b moved\n"})
        with mock.patch.object(safe_push, "_dry", side_effect=broker.BrokerDenied(
                "merge of the base could not be computed")) as dry:
            self.assertEqual(self.parents(self.push()), [self.base])
        dry.assert_called_once()

    def test_a_bad_merge_is_still_refused(self):
        # Only computing the merge may fail quietly; a merge built with the wrong parents denies.
        self.move_main({"b.txt": "b moved\n"})
        real = safe_push._Isolated.run

        def run(git, *args, **kwargs):
            out = real(git, *args, **kwargs)
            if "rev-list" in args and len(out.split()) == 3:
                return out.split()[0] + b" " + out.split()[2]      # the fix parent dropped
            return out
        with mock.patch.object(safe_push._Isolated, "run", run), \
                self.assertRaisesRegex(broker.BrokerDenied, "expected parents"):
            self.push()
        self.assertEqual(subprocess.run(["git", "--git-dir", self.remote, "rev-parse", "--verify",
                                         "-q", f"refs/heads/{BRANCH}"], env=ENV,
                                        capture_output=True).returncode, 1)

    def test_an_unreadable_base_still_denies_the_push(self):
        # The pre-existing check that the scoped base is on the advertised branch stays: with no
        # base to read, nothing is built or pushed (maintainer decision on #2591).
        self.git("--git-dir", self.remote, "update-ref", "-d", "refs/heads/main")
        with self.assertRaises(broker.BrokerDenied):
            self.push()
        self.assertEqual(subprocess.run(["git", "--git-dir", self.remote, "rev-parse", "--verify",
                                         "-q", f"refs/heads/{BRANCH}"], env=ENV,
                                        capture_output=True).returncode, 1)

    def test_nothing_is_merged_when_the_base_did_not_move(self):
        self.assertEqual(self.parents(self.push()), [self.base])


class OpenBranchCatchesUp(Fixture):
    """open_branch, end to end on the local remote, for a loop that never set update_behind."""

    def test_open_branch_publishes_the_merged_head(self):
        import base64
        import hashlib
        tip = self.move_main({"b.txt": "b moved\n"})
        data = b"fixed\n"
        manifest = {"base_head": self.base, "message": "Fix the typo",
                    "files": [{"path": "a.txt", "content_b64": base64.b64encode(data).decode(),
                               "sha256": hashlib.sha256(data).hexdigest()}]}
        audit, real_cas = [], safe_push._git_cas

        def observed(*args, **kwargs):
            return self.git("--git-dir", self.remote, "rev-parse", f"refs/heads/{BRANCH}")
        with mock.patch.object(config, "issue_fixes_enabled", return_value=True), \
                mock.patch.object(broker, "authorize_issue_fix", return_value="fix"), \
                mock.patch.object(safe_push, "_fixer_identity",
                                  return_value={"name": "fix", "email": "fix@example.org"}), \
                mock.patch.object(safe_push, "_absent_branch"), \
                mock.patch.object(safe_push, "_audit", side_effect=lambda loop, row: audit.append(row)), \
                mock.patch.object(safe_push, "_confirm_ref", side_effect=observed), \
                mock.patch.object(safe_push, "_git_cas",
                                  lambda *a, **k: real_cas(*a, remote=self.remote, **k)):
            result = safe_push.open_branch({**self.loop, "repo": REPO}, repo=REPO, number=12,
                                           base=self.base, branch=BRANCH, manifest=manifest)
        head = result["new_head"]
        self.assertEqual((result["outcome"], self.parents(head)[1]), ("published", tip))
        self.assertEqual([row["new_head"] for row in audit], [head, head])


class Doctor(unittest.TestCase):
    """#2602: on a base that never reports a PR ``behind``, ``update_behind``'s sweep merges
    nothing, but the catch-up above still runs, so the doctor does not say the host merges nothing."""
    LOOP = {"id": "w", "repo": REPO, "base": "main", "update_behind": True,
            "unattended_fixer_push": True, "seats": {"fixer": {"login": "fix"}}}

    def detail(self, **loop):
        from diaktoros import doctor, gh
        from diaktoros.doctor import identity_checks
        with mock.patch.object(gh, "api", return_value={"permissions": {"push": True}}), \
                mock.patch.object(doctor, "base_requires_up_to_date", return_value=(False, "")):
            check = identity_checks.check_update_behind({**self.LOOP, **loop})
        self.assertEqual(check.status, doctor.UNKNOWN)
        return check.detail

    def test_a_base_not_requiring_up_to_date_branches_still_leaves_the_issue_fix_catch_up(self):
        on = self.detail(triage={"route": "w-t", "fix_label": "fix-me"})
        self.assertIn("merges nothing into an open loop PR", on)
        self.assertIn("issue-fix branch still gets a clean base merge", on)
        self.assertNotIn("issue-fix", self.detail())        # issue fixes off: nothing to except


if __name__ == "__main__":
    unittest.main()
