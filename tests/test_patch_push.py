#!/usr/bin/env python3
"""#64: a fix to a large file, a deletion, a rename — published as a diff, checked like whole files.

Live, an issue fix that had to edit a 108 KB file could not be published: a push carried whole
files of at most 64 KiB, and could not delete. Ten files in this repository, and the core of
most real ones, were out of a fixer's reach. Now the client sends a unified diff of /work against
the read-only export of the same head when whole files do not fit; the host applies it with Git
to the index of a fresh copy of that exact head and checks every path it changed.

These tests go through real Git end to end: the diff the client builds, applied by the host's
own `_git_cas`, must reproduce /work exactly — including a file with no final newline, a new
file, a deletion and a large file — and every adversarial diff must be refused before the push.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import base64
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, broker_client, safe_push  # noqa: E402

REPO = "acme/widgets"
BIG = "".join(f"line {n}\n" for n in range(20000))           # ~200 KB of text


def git(*args, **kwargs):
    return subprocess.check_output(["git", *args], text=True, **kwargs).strip()


class RoundTrip(unittest.TestCase):
    """A real base commit on a local remote; the client's diff; the host's `_git_cas`."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.export = self.root / "export"
        base_files = {"src/big.py": BIG, "src/small.py": "x = 1\n", "README.md": "readme",
                      "old/gone.txt": "bye\n", "run.sh": "#!/bin/sh\necho hi\n"}
        for path, text in base_files.items():
            (self.export / path).parent.mkdir(parents=True, exist_ok=True)
            (self.export / path).write_text(text)
        (self.export / "run.sh").chmod(0o755)
        (self.export / "link").symlink_to("README.md")
        self.work = self.root / "work"
        subprocess.check_call(["cp", "-a", f"{self.export}/.", str(self.work)])
        # The base commit, from the export, on a bare "remote".
        self.remote = str(self.root / "remote.git")
        src = self.root / "src"
        subprocess.check_call(["cp", "-a", f"{self.export}/.", str(src)])
        env = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
               "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}
        git("init", "-q", str(src))
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "base", env=env)
        git("init", "-q", "--bare", self.remote)
        self.base = git("-C", str(src), "rev-parse", "HEAD")
        git("-C", str(src), "push", "-q", self.remote, "HEAD:refs/heads/fix-7")
        self.token = self.root / "token"
        self.token.write_text("not-a-real-token")
        env_patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/does/not/exist"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def manifest(self, files):
        turn = self.root / "turn.json"
        turn.write_text('{"head": "%s"}' % self.base)
        return broker_client.build_manifest(files, "Fix it", work=str(self.work),
                                            export=str(self.export), turn_file=str(turn))

    def publish(self, manifest):
        """The host side, as `push` runs it: validate, then `_git_cas` against the remote."""
        base, files, patch = safe_push._manifest(manifest)
        self.assertEqual(base, self.base)
        changed = []
        extra = {"patch": patch, "changed": changed} if patch is not None else {}
        new = safe_push._git_cas({"tokens": {"fix": str(self.token)}}, REPO, "fix-7", self.base,
                                 files, "Fix it", "fix",
                                 {"name": "fix", "email": "3+fix@users.noreply.github.com"},
                                 remote=self.remote, **extra)
        return new, changed

    def tree(self, commit):
        return {line.split("\t")[1]: line.split()[0]
                for line in git("--git-dir", self.remote, "ls-tree", "-r", commit).splitlines()}

    def show(self, commit, path):
        return subprocess.check_output(["git", "--git-dir", self.remote, "show",
                                        f"{commit}:{path}"])

    def test_small_files_still_go_whole(self):
        (self.work / "src/small.py").write_text("x = 2\n")
        manifest = self.manifest(["src/small.py"])
        self.assertIn("files", manifest)
        new, _ = self.publish(manifest)
        self.assertEqual(self.show(new, "src/small.py"), b"x = 2\n")

    def test_an_edit_a_new_file_and_a_deletion_reproduce_work_exactly(self):
        edited = BIG.replace("line 12345\n", "line 12345 fixed\n")
        (self.work / "src/big.py").write_text(edited)
        (self.work / "README.md").write_text("readme, no final newline still")   # no "\n"
        (self.work / "src/new.py").write_text("print('new')\n")
        (self.work / "old/gone.txt").unlink()
        (self.work / "run.sh").write_text("#!/bin/sh\necho hello\n")
        files = ["src/big.py", "README.md", "src/new.py", "old/gone.txt", "run.sh"]
        manifest = self.manifest(files)
        self.assertEqual(set(manifest), {"base_head", "message", "patch_b64", "sha256"})
        patch = base64.b64decode(manifest["patch_b64"])
        self.assertLess(len(patch), 4000, "a one-line fix to a 200 KB file is a small diff")
        self.assertEqual(sorted(broker_client.manifest_paths(manifest)), sorted(files))
        new, changed = self.publish(manifest)
        self.assertEqual(sorted(changed), sorted(files))
        self.assertEqual(self.show(new, "src/big.py").decode(), edited)
        self.assertEqual(self.show(new, "README.md"), b"readme, no final newline still")
        self.assertEqual(self.show(new, "src/new.py"), b"print('new')\n")
        tree = self.tree(new)
        self.assertNotIn("old/gone.txt", tree)
        self.assertEqual(tree["run.sh"], "100755")        # an edited script keeps its mode
        self.assertEqual(tree["link"], "120000")          # untouched symlink stays as it was
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), new)
        self.assertEqual(git("--git-dir", self.remote, "rev-list", "--parents", "-n", "1", new),
                         f"{new} {self.base}")

    def test_a_rename_is_the_old_path_and_the_new_one(self):
        (self.work / "src/small.py").rename(self.work / "src/renamed.py")
        manifest = self.manifest(["src/small.py", "src/renamed.py"])
        self.assertIn("patch_b64", manifest)                # a deletion needs a diff
        new, _ = self.publish(manifest)
        tree = self.tree(new)
        self.assertNotIn("src/small.py", tree)
        self.assertEqual(self.show(new, "src/renamed.py"), b"x = 1\n")

    def raw_patch(self, text):
        data = text.encode()
        return {"base_head": self.base, "message": "Fix it",
                "patch_b64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest()}

    def test_adversarial_diffs_are_refused_before_any_push(self):
        cases = {
            "a workflow": ("diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
                           "new file mode 100644\n--- /dev/null\n+++ b/.github/workflows/ci.yml\n"
                           "@@ -0,0 +1 @@\n+on: push\n"),
            "codeowners": ("diff --git a/CODEOWNERS b/CODEOWNERS\nnew file mode 100644\n"
                           "--- /dev/null\n+++ b/CODEOWNERS\n@@ -0,0 +1 @@\n+* @me\n"),
            "a new symlink": ("diff --git a/evil b/evil\nnew file mode 120000\n--- /dev/null\n"
                              "+++ b/evil\n@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n"),
            "editing a symlink": ("diff --git a/link b/link\n--- a/link\n+++ b/link\n@@ -1 +1 @@\n"
                                  "-README.md\n\\ No newline at end of file\n+/etc/passwd\n"
                                  "\\ No newline at end of file\n"),
            "a submodule": ("diff --git a/sub b/sub\nnew file mode 160000\nindex 0000000..1111111\n"
                            "--- /dev/null\n+++ b/sub\n@@ -0,0 +1 @@\n"
                            "+Subproject commit 1111111111111111111111111111111111111111\n"),
            "outside the tree": ("diff --git a/../x b/../x\nnew file mode 100644\n--- /dev/null\n"
                                 "+++ b/../x\n@@ -0,0 +1 @@\n+x\n"),
            "a stale hunk": ("diff --git a/src/small.py b/src/small.py\n--- a/src/small.py\n"
                             "+++ b/src/small.py\n@@ -1 +1 @@\n-x = 99\n+x = 2\n"),
            "a mode change": ("diff --git a/src/small.py b/src/small.py\nold mode 100644\n"
                              "new mode 100755\n"),
            "nothing": ("diff --git a/src/small.py b/src/small.py\n"),
        }
        before = git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7")
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(broker.BrokerDenied):
                self.publish(self.raw_patch(text))
        self.assertEqual(git("--git-dir", self.remote, "rev-parse", "refs/heads/fix-7"), before)


class Validation(unittest.TestCase):
    def test_a_diff_manifest_is_checked_like_a_file_manifest(self):
        data = b"diff --git a/x b/x\n"
        good = {"base_head": "a" * 40, "message": "m",
                "patch_b64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest()}
        self.assertEqual(safe_push._manifest(good), ("a" * 40, [], data))
        bad = {
            "wrong digest": {**good, "sha256": "0" * 64},
            "both shapes": {**good, "files": []},
            "empty": {**good, "patch_b64": "", "sha256": hashlib.sha256(b"").hexdigest()},
            "too large": {**good, "patch_b64": "A" * (4 * ((safe_push.MAX_PATCH + 2) // 3) + 4)},
            "not base64": {**good, "patch_b64": "!!!"},
        }
        for name, manifest in bad.items():
            with self.subTest(name), self.assertRaises(broker.BrokerDenied):
                safe_push._manifest(manifest)

    def test_the_client_refuses_what_has_no_diff_form(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            work, export = Path(temp) / "work", Path(temp) / "export"
            work.mkdir()
            export.mkdir()
            (work / "same.txt").write_text("a\n")
            (export / "same.txt").write_text("a\n")
            (work / "big.bin").write_bytes(b"\0" * (safe_push.MAX_FILE + 1))
            (work / "huge.txt").write_text("y\n" * (safe_push.MAX_PATCH // 2 + 10))
            for files, expect in ((["big.bin"], "binary"), (["huge.txt"], "at most"),
                                  (["same.txt", "big.bin"], "binary")):
                with self.subTest(files=files), self.assertRaisesRegex(
                        broker_client.ManifestError, expect):
                    broker_client.build_manifest(files, "m", work=str(work), export=str(export),
                                                 turn_file=str(Path(temp) / "t"))


if __name__ == "__main__":
    unittest.main()
