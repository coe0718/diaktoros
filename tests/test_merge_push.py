"""#303 stage 3, host side: merge the base into a conflicted PR head, export the conflicted tree,
and push the seat's resolution as a two-parent merge commit under the exact-head lease.

Real Git, real local bare repository: the PR branch and the base both change ``a.py`` (a real
conflict); the base also adds ``c.py`` and a workflow file (clean changes from the base, which the
seat never wrote); the PR adds ``new.py``. Nothing here reaches the network.
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
from review_loop import broker, broker_client, safe_push, trusted_fetch  # noqa: E402

REPO = "acme/widgets"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
       "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}


def git(*args):
    return subprocess.check_output(["git", *args], env=ENV, text=True).strip()


class Merge(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        src = self.root / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", str(src))
        (src / "a.py").write_text("x = 1\n")
        (src / "b.py").write_text("keep\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "base")
        git("-C", str(src), "checkout", "-qb", "fix-7")
        (src / "a.py").write_text("x = 2\n")
        (src / "new.py").write_text("print('pr')\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "the PR")
        self.head = git("-C", str(src), "rev-parse", "HEAD")
        git("-C", str(src), "checkout", "-q", "main")
        (src / "a.py").write_text("x = 3\n")
        (src / "c.py").write_text("print('main')\n")
        (src / ".github" / "workflows").mkdir(parents=True)
        (src / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "main moved")
        self.base = git("-C", str(src), "rev-parse", "HEAD")
        self.remote = str(self.root / "remote.git")
        git("init", "-q", "--bare", self.remote)
        git("-C", str(src), "push", "-q", self.remote, "main", "fix-7")
        token = self.root / "token"
        token.write_text("not-a-real-token")
        self.loop = {"repo": REPO, "tokens": {"fix": str(token), "read": str(token)}}
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/does/not/exist"})
        env.start()
        self.addCleanup(env.stop)

    def merged(self, head=None, base=None):
        return safe_push.merged_tree(self.loop, branch="fix-7", head=head or self.head,
                                     base_ref="main", base_sha=base or self.base, login="read",
                                     remote=self.remote)

    def export(self, merged):
        """As the host stages it: the archive, verified entry by entry, into a fresh directory."""
        stage = Path(tempfile.mkdtemp(dir=self.root))
        export = stage / "export"
        export.mkdir()
        trusted_fetch._extract(merged["archive"], export, merged["entries"], merged["skipped"])
        work = stage / "work"
        subprocess.check_call(["cp", "-a", f"{export}/.", str(work)])
        return export, work

    def resolve(self, merged, edits, *, scope=None):
        export, work = self.export(merged)
        for path, text in edits.items():
            (work / path).write_text(text)
        turn = work.parent / "turn.json"
        turn.write_text('{"head": "%s"}' % self.head)
        manifest = broker_client.build_manifest(list(edits), "Merge main", work=str(work),
                                                export=str(export), turn_file=str(turn))
        _, files, patch = safe_push._manifest(manifest)
        extra = {"patch": patch} if patch is not None else {}
        return safe_push._git_cas(
            self.loop, REPO, "fix-7", self.head, files, "Merge main", "fix",
            {"name": "fix", "email": "3+fix@users.noreply.github.com"}, remote=self.remote,
            merge=scope or {"base_ref": "main", "base_sha": self.base, "tree": merged["tree"]},
            **extra)

    def test_the_export_carries_the_conflict_and_both_sides(self):
        merged = self.merged()
        self.assertEqual(merged["conflicted"], ["a.py"])
        export, _ = self.export(merged)
        text = (export / "a.py").read_text()
        self.assertIn("<<<<<<<", text)
        self.assertIn("x = 2", text)
        self.assertIn("x = 3", text)
        self.assertEqual((export / "c.py").read_text(), "print('main')\n")       # from main
        self.assertEqual((export / "new.py").read_text(), "print('pr')\n")       # from the PR
        self.assertTrue((export / ".github" / "workflows" / "ci.yml").exists())
        sides = merged["sides"]
        self.assertIn("What the PR changed", sides)
        self.assertIn("+x = 2", sides)
        self.assertIn("What the base changed", sides)
        self.assertIn("+x = 3", sides)
        self.assertNotIn("c.py", sides)                     # only the conflicted files

    def test_a_resolution_becomes_a_two_parent_merge_under_the_lease(self):
        merged = self.merged()
        new = self.resolve(merged, {"a.py": "x = 3  # main's value, the PR's intent noted\n"})
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), new)
        self.assertEqual(git("--git-dir", self.remote, "rev-list", "--parents", "-n", "1", new)
                         .split(), [new, self.head, self.base])
        show = lambda path: git("--git-dir", self.remote, "show", f"{new}:{path}")  # noqa: E731
        self.assertEqual(show("a.py"), "x = 3  # main's value, the PR's intent noted")
        self.assertEqual(show("c.py"), "print('main')")
        self.assertEqual(show("new.py"), "print('pr')")
        # The base's own workflow change rides along; the seat never wrote it.
        self.assertEqual(show(".github/workflows/ci.yml"), "on: push")

    def test_markers_left_are_refused_before_anything_moves(self):
        merged = self.merged()
        with self.assertRaisesRegex(broker.BrokerDenied, r"conflict markers left in 1 file\(s\)"):
            self.resolve(merged, {"b.py": "keep, and nothing resolved\n"})
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), self.head)

    def test_the_seat_still_cannot_write_a_control_file(self):
        merged = self.merged()
        # The client refuses it before sending anything …
        with self.assertRaisesRegex(broker_client.ManifestError, "repository control file"):
            self.resolve(merged, {"a.py": "x = 3\n", ".github/workflows/ci.yml": "on: [push, pr]\n"})
        # … and the host refuses a hand-built manifest that tries anyway.
        import base64
        import hashlib
        data = b"on: [push, pr]\n"
        manifest = {"base_head": self.head, "message": "m", "files": [
            {"path": ".github/workflows/ci.yml", "content_b64": base64.b64encode(data).decode(),
             "sha256": hashlib.sha256(data).hexdigest()}]}
        with self.assertRaisesRegex(broker.BrokerDenied, "repository control file"):
            safe_push._manifest(manifest)

    def test_a_moved_merge_a_moved_head_or_an_alien_base_is_refused(self):
        merged = self.merged()
        with self.assertRaisesRegex(broker.BrokerDenied, "changed since the turn was staged"):
            self.resolve(merged, {"a.py": "x = 3\n"},
                         scope={"base_ref": "main", "base_sha": self.base, "tree": "e" * 40})
        with self.assertRaisesRegex(broker.BrokerDenied, "fetched PR branch moved"):
            self.merged(head="d" * 40)
        with self.assertRaisesRegex(broker.BrokerDenied, "invalid merge scope"):
            self.resolve(merged, {"a.py": "x = 3\n"}, scope={"base_sha": self.base, "tree": "x"})
        with self.assertRaisesRegex(broker.BrokerDenied, "not on the base branch"):
            self.merged(base=self.head)                # a real commit, but not on main
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), self.head)

    def test_a_clean_merge_has_no_conflicted_paths(self):
        src = self.root / "clean"
        git("clone", "-q", self.remote, str(src))
        git("-C", str(src), "checkout", "-q", "main")
        git("-C", str(src), "reset", "-q", "--hard", f"{self.base}~1")
        (src / "d.py").write_text("print('d')\n")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "unrelated")
        git("-C", str(src), "push", "-q", "-f", self.remote, "HEAD:refs/heads/main")
        clean_base = git("-C", str(src), "rev-parse", "HEAD")
        self.assertEqual(self.merged(base=clean_base)["conflicted"], [])


    def test_a_7_equals_underline_in_a_resolution_is_not_a_marker(self):
        """Tuck's P3: a Markdown/RST underline of 7 '=' was refused as a marker."""
        merged = self.merged()
        new = self.resolve(merged, {"a.py": "x = 3\n# Title\n# =======\n=======\n"})
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), new)

    def test_the_workflow_flag_says_when_the_base_brings_ci_changes(self):
        self.assertTrue(self.merged()["workflows"])     # main added .github/workflows/ci.yml


class WholeFile(unittest.TestCase):
    """Tuck's blocker on #408: modify/delete and binary conflicts leave no markers, so a turn that
    never touched them would silently drop main's side. They are refused: a person resolves them."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        src = self.root / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", str(src))
        (src / "a.py").write_text("x = 1\n")
        (src / "gone.txt").write_text("base\n")
        (src / "img.bin").write_bytes(b"\x00\x01base")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "base")
        git("-C", str(src), "checkout", "-qb", "fix-7")
        (src / "a.py").write_text("x = 2\n")
        (src / "gone.txt").write_text("pr edit\n")
        (src / "img.bin").write_bytes(b"\x00\x02pr")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "pr")
        self.head = git("-C", str(src), "rev-parse", "HEAD")
        git("-C", str(src), "checkout", "-q", "main")
        (src / "a.py").write_text("x = 3\n")
        git("-C", str(src), "rm", "-q", "gone.txt")
        (src / "img.bin").write_bytes(b"\x00\x03main")
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "main")
        self.base = git("-C", str(src), "rev-parse", "HEAD")
        self.remote = str(self.root / "remote.git")
        git("init", "-q", "--bare", self.remote)
        git("-C", str(src), "push", "-q", self.remote, "main", "fix-7")
        token = self.root / "token"
        token.write_text("not-a-real-token")
        self.loop = {"repo": REPO, "tokens": {"fix": str(token), "read": str(token)}}
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/does/not/exist"})
        env.start()
        self.addCleanup(env.stop)

    def test_staging_refuses_and_names_it_a_persons_job(self):
        with self.assertRaisesRegex(safe_push.NeedsPerson, "2 whole-file conflict"):
            safe_push.merged_tree(self.loop, branch="fix-7", head=self.head, base_ref="main",
                                  base_sha=self.base, login="read", remote=self.remote)

    def test_a_push_resolving_only_the_text_file_is_refused_and_the_branch_unmoved(self):
        files = [("a.py", b"x = 3\n")]
        with self.assertRaises(safe_push.NeedsPerson):
            safe_push._git_cas(self.loop, REPO, "fix-7", self.head, files, "m", "fix",
                               {"name": "fix", "email": "3+fix@users.noreply.github.com"},
                               remote=self.remote,
                               merge={"base_ref": "main", "base_sha": self.base, "tree": "a" * 40})
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), self.head)

    def test_a_marker_lookalike_does_not_disguise_a_modify_delete(self):
        """The PR's surviving file happens to hold a line that looks like a marker: the marker
        scan alone would take it for a content conflict. The missing stage still refuses it."""
        src = self.root / "look"
        git("clone", "-q", "-b", "fix-7", self.remote, str(src))
        (src / "gone.txt").write_text("<<<<<<< this is just text\n")
        (src / "img.bin").write_bytes(b"\x00\x03main")     # the same as main: no binary conflict
        git("-C", str(src), "commit", "-qam", "lookalike")
        git("-C", str(src), "push", "-q", "origin", "fix-7")
        head = git("-C", str(src), "rev-parse", "HEAD")
        with self.assertRaisesRegex(safe_push.NeedsPerson, "whole-file"):
            safe_push.merged_tree(self.loop, branch="fix-7", head=head, base_ref="main",
                                  base_sha=self.base, login="read", remote=self.remote)

    def test_a_binary_kept_whole_is_refused_even_with_a_marker_shaped_line(self):
        """#409 (Tuck): both sides change a binary; the kept side's bytes contain a line that
        looks like a marker, so a marker scan alone calls it a content conflict."""
        src = self.root / "sneaky"
        git("clone", "-q", "-b", "fix-7", self.remote, str(src))
        (src / "gone.txt").write_text("base\n")                  # no modify/delete this time
        (src / "img.bin").write_bytes(b"\x00\x02pr\n<<<<<<< sneaky\n")
        git("-C", str(src), "commit", "-qam", "sneaky binary")
        git("-C", str(src), "push", "-q", "origin", "fix-7")
        head = git("-C", str(src), "rev-parse", "HEAD")
        main = self.root / "main-gone"
        git("clone", "-q", "-b", "main", self.remote, str(main))
        (main / "gone.txt").write_text("base\n")                 # restore it on main as well
        git("-C", str(main), "add", "gone.txt")
        git("-C", str(main), "commit", "-qm", "keep gone.txt")
        git("-C", str(main), "push", "-q", "origin", "main")
        base = git("-C", str(main), "rev-parse", "HEAD")
        with self.assertRaisesRegex(safe_push.NeedsPerson, "1 whole-file"):
            safe_push.merged_tree(self.loop, branch="fix-7", head=head, base_ref="main",
                                  base_sha=base, login="read", remote=self.remote)

    def test_a_bad_base_branch_name_is_refused(self):
        with self.assertRaisesRegex(broker.BrokerDenied, "invalid base branch name"):
            safe_push.merged_tree(self.loop, branch="fix-7", head=self.head, base_ref="main..x",
                                  base_sha=self.base, login="read", remote=self.remote)

if __name__ == "__main__":
    unittest.main()
