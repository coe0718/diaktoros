"""Duplication audit (B6): every file the plugin publishes goes through ``util.atomic_write``.

About a dozen writers repeated temp file, fsync, replace, directory fsync by hand, and some
skipped steps: the loop-config writer and the gate shims never synced the directory, the runtime
file and the pacing state were never fsynced and used one fixed ``.tmp`` name, so two writers
could publish each other's half-written file. Now the rule has one home; the writers that differ
on purpose (the route registry's own durability error, a streamed restore, migration renames, log
rotation, a cache directory swap) are named in ``DIFFERENT``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import util  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Writers that publish by rename on their own, each for a reason the helper does not cover.
DIFFERENT = {
    "diaktoros/util.py": "the helper itself",
    "diaktoros/routes.py": "RegistryDurabilityError: published but the directory sync failed",
    "diaktoros/backup.py": "a restore streams a member to its target",
    "diaktoros/migrate.py": "renames a loop's files; nothing is written",
    "diaktoros/run_supervisor/workers.py": "rotates a worker log",
    "diaktoros/deps.py": "retires and restores a dependency-cache directory",
}


class Helper(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "state.json"

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p != self.path)

    def test_publishes_private_then_syncs_the_file_and_then_its_directory(self):
        real, seen = os.fsync, []

        def spy(fd):
            seen.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real(fd)

        with mock.patch.object(util.os, "fsync", spy):
            util.atomic_write(self.path, "text")
        self.assertEqual(seen, ["file", "dir"])
        self.assertEqual(self.path.read_text(), "text")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        util.atomic_write(self.path, b"bytes", mode=0o755)
        self.assertEqual((self.path.read_bytes(), stat.S_IMODE(self.path.stat().st_mode)),
                         (b"bytes", 0o755))
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_check_or_write_publishes_nothing_and_leaves_no_temp_file(self):
        self.path.write_text("before")

        def refuse():
            raise OSError("changed meanwhile")

        with self.assertRaises(OSError):
            util.atomic_write(self.path, "after", before_replace=refuse)
        with mock.patch.object(util.os, "write", return_value=0), self.assertRaises(OSError):
            util.atomic_write(self.path, "after")              # a write that makes no progress
        self.assertEqual(self.path.read_text(), "before")
        self.assertEqual(self.leftovers(), [])

    def test_two_writers_never_share_a_temp_file(self):
        names = []
        real = tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, name = real(*args, **kwargs)
            names.append(name)
            return fd, name

        with mock.patch.object(tempfile, "mkstemp", spy):
            util.atomic_write(self.path, "one")
            util.atomic_write(self.path, "two")
        self.assertEqual(len(set(names)), 2)


class OneHome(unittest.TestCase):
    def test_no_writer_publishes_by_rename_outside_the_helper(self):
        for path in sorted([*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]):
            rel = path.relative_to(ROOT).as_posix()
            if rel in DIFFERENT:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                func = node.func
                named = ast.unparse(func)
                with self.subTest(f"{rel}:{node.lineno} {named}"):
                    self.assertNotIn(named, {"os.replace", "os.rename", "tempfile.mkstemp",
                                             "tempfile.NamedTemporaryFile"})
                    # Path.replace / Path.rename onto another path (str.replace takes two args).
                    if func.attr in ("replace", "rename") and len(node.args) == 1:
                        self.fail(f"{rel}:{node.lineno}: {named}(...) publishes by hand")

    def test_every_exception_still_has_its_reason(self):
        for rel, why in DIFFERENT.items():
            with self.subTest(rel):
                self.assertTrue((ROOT / rel).is_file())
                self.assertRegex((ROOT / rel).read_text(), r"\.(replace|rename)\(")
                self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
