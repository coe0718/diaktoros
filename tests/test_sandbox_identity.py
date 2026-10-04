"""The sandbox's uid has a user entry (#240), and it is the sandbox's own, never the host's.

Found on the first live review (#238): bubblewrap keeps the host uid but the sandbox had no user
database, so ``pwd.getpwuid`` failed inside the turn and the reviewer could not run this repo's
tests (it improvised a shim). ``contained.run`` now stages the sandbox's whole ``/etc`` per launch
— one user, one group, and an empty mount point for the alternatives symlink farm — and binds it
read-only; nothing else from the host's ``/etc`` is mounted.
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
         "len(pwd.getpwall()), len(grp.getgrall()))")


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

    def etc(self) -> Path:
        directory = self.root / "etc"
        directory.mkdir()
        return contained.write_etc(directory, uid=1000, gid=1000)


class Command(Layout):
    def test_the_sandbox_etc_is_the_staged_directory_and_nothing_from_the_host(self):
        etc = self.etc()
        argv = contained.command(**self.dirs, entry=["/bin/true"], etc_dir=etc,
                                 checkout_writable=False)
        binds = [(f, s, d) for f, s, d in zip(argv, argv[1:], argv[2:])
                 if f in ("--ro-bind", "--ro-bind-try", "--bind")]
        self.assertIn(("--ro-bind", str(etc), "/etc"), binds)
        # The staged /etc first, then the host's alternatives symlink farm onto its empty mount
        # point; no other bind targets anything under /etc, and none takes a host /etc source.
        self.assertLess(binds.index(("--ro-bind", str(etc), "/etc")),
                        binds.index(("--ro-bind-try", "/etc/alternatives", "/etc/alternatives")))
        for flag, source, target in binds:
            if target == "/etc" or target.startswith("/etc/"):
                self.assertIn((source, target), {(str(etc), "/etc"),
                                                 ("/etc/alternatives", "/etc/alternatives")})
        self.assertEqual((etc / "passwd").read_text(),
                         "agent:x:1000:1000:review-loop seat:/home/agent:/bin/sh\n")
        self.assertEqual((etc / "group").read_text(), "agent:x:1000:\n")
        self.assertEqual((etc / "passwd").stat().st_mode & 0o777, 0o444)
        self.assertEqual(sorted(p.name for p in etc.iterdir()), ["alternatives", "group", "passwd"])

    def test_a_staged_etc_holding_anything_else_is_refused(self):
        for extra in ("shadow", "alternatives/x"):
            with self.subTest(extra=extra):
                etc = self.root / f"etc-{extra.replace('/', '-')}"
                etc.mkdir()
                contained.write_etc(etc, uid=1000, gid=1000)
                (etc / "alternatives").chmod(0o755)
                (etc / extra).write_text("x\n")
                with self.assertRaisesRegex(ValueError, "only the staged user and group"):
                    contained.command(**self.dirs, entry=["/bin/true"], etc_dir=etc)


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
        name, home, getuser, group, users, groups = result.stdout.split()
        self.assertEqual((name, home, getuser, group), ("agent", "/home/agent", "agent", "agent"))
        # One user and one group: the host's user database never reaches the sandbox.
        self.assertEqual((users, groups), ("1", "1"))


    def test_multiprocessing_works_and_dev_stays_read_only(self):
        """#297: a seat's tests could not create a multiprocessing lock — /dev/shm sat on the
        read-only /dev. A sized tmpfs there makes it writable; /dev itself stays sealed."""
        probe = ("import multiprocessing as m, os\n"
                 "lock = m.Lock(); queue = m.Queue(); queue.put(41); lock.acquire(); lock.release()\n"
                 "print('queue', queue.get() + 1)\n"
                 "for path in ('/dev/shm/probe', '/dev/probe'):\n"
                 "    try:\n"
                 "        open(path, 'w').close(); print(path, 'writable')\n"
                 "    except OSError as exc:\n"
                 "        print(path, 'refused', exc.errno)\n")
        try:
            result = contained.run(**self.dirs, entry=["/usr/bin/python3", "-c", probe],
                                   checkout_writable=False, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(f"bubblewrap could not start here: {exc}")
        if result.returncode != 0 and "namespace" in result.stderr.lower():
            self.skipTest(f"no unprivileged namespaces here: {result.stderr.strip()[:200]}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("queue 42", result.stdout)
        self.assertIn("/dev/shm/probe writable", result.stdout)
        self.assertIn("/dev/probe refused 30", result.stdout)          # EROFS: still sealed


if __name__ == "__main__":
    unittest.main()
