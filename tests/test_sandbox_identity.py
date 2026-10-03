"""The sandbox's uid has a user entry (#240), and it is the sandbox's own, never the host's.

Found on the first live review (#238): bubblewrap keeps the host uid but mounted no /etc/passwd,
so ``pwd.getpwuid`` failed inside the turn and the reviewer could not run this repo's tests (it
improvised a shim). ``contained.run`` now writes a one-line ``passwd`` and ``group`` per launch
and binds them read-only; nothing from the host's /etc is mounted.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_loop import contained  # noqa: E402

PROBE = ("import getpass, grp, os, pwd; user = pwd.getpwuid(os.getuid()); "
         "print(user.pw_name, user.pw_dir, getpass.getuser(), grp.getgrgid(os.getgid()).gr_name, "
         "sum(1 for _ in open('/etc/passwd')))")


class Layout(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dirs = {}
        for name in ("code", "venv", "runtime", "home", "checkout", "rust"):
            (self.root / name).mkdir()
            self.dirs[name] = self.root / name
        self.dirs["query"] = self.root / "query"
        self.dirs["query"].write_text("q\n")

    def identity(self) -> Path:
        directory = self.root / "identity"
        directory.mkdir()
        return contained.write_identity(directory, uid=1000, gid=1000)


class Command(Layout):
    def test_the_identity_files_are_bound_read_only_and_nothing_else_from_etc(self):
        identity = self.identity()
        argv = contained.command(**self.dirs, entry=["/bin/true"], identity_dir=identity,
                                 checkout_writable=False)
        self.assertIn(("--ro-bind", str(identity / "passwd"), "/etc/passwd"),
                      list(zip(argv, argv[1:], argv[2:])))
        self.assertIn(("--ro-bind", str(identity / "group"), "/etc/group"),
                      list(zip(argv, argv[1:], argv[2:])))
        # Never a host file as the source: every bind whose target is under /etc is the staged
        # identity or the alternatives symlink farm.
        for flag, source, target in zip(argv, argv[1:], argv[2:]):
            if flag in ("--ro-bind", "--ro-bind-try", "--bind") and target.startswith("/etc"):
                self.assertTrue(source.startswith(str(identity)) or target == "/etc/alternatives",
                                (flag, source, target))
        self.assertEqual((identity / "passwd").read_text(),
                         "agent:x:1000:1000:review-loop seat:/home/agent:/bin/sh\n")
        self.assertEqual((identity / "passwd").stat().st_mode & 0o777, 0o444)

    def test_an_identity_dir_holding_anything_else_is_refused(self):
        identity = self.identity()
        (identity / "shadow").write_text("x\n")
        with self.assertRaisesRegex(ValueError, "only the staged passwd and group"):
            contained.command(**self.dirs, entry=["/bin/true"], identity_dir=identity)


@unittest.skipUnless(shutil.which("bwrap") and Path("/usr/bin/python3").exists(),
                     "needs bubblewrap and /usr/bin/python3")
class RealSandbox(Layout):
    def test_the_seat_has_a_user_entry_and_it_is_its_own(self):
        try:
            result = contained.run(**self.dirs, entry=["/usr/bin/python3", "-c", PROBE],
                                   checkout_writable=False, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:   # a host that cannot start bwrap
            self.skipTest(f"bubblewrap could not start here: {exc}")
        if result.returncode != 0 and "namespace" in result.stderr.lower():
            self.skipTest(f"no unprivileged namespaces here: {result.stderr.strip()[:200]}")
        self.assertEqual(result.returncode, 0, result.stderr)
        name, home, getuser, group, lines = result.stdout.split()
        self.assertEqual((name, home, getuser, group), ("agent", "/home/agent", "agent", "agent"))
        self.assertEqual(lines, "1", "the host's /etc/passwd reached the sandbox")


if __name__ == "__main__":
    unittest.main()
