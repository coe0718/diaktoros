"""Host Node detection and its doctor line (#685)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import contained, doctor, runtime_detect  # noqa: E402
from test_sandbox_limits import bwrap_works  # noqa: E402


class NodeRuntime(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"HERMES_REAL_HOME": str(self.home), "PATH": "/nonexistent"})
        env.start()
        self.addCleanup(env.stop)

    def install(self, root: Path, version: str = "v22.3.0") -> Path:
        (root / "bin").mkdir(parents=True)
        node = root / "bin" / "node"
        node.write_text(f"#!/bin/sh\necho {version}\n")
        node.chmod(0o755)
        return root

    def test_finds_hermes_home_node_behind_a_user_local_link(self):
        root = self.install(self.home / ".hermes" / "node")
        link = self.home / ".local" / "bin"
        link.mkdir(parents=True)
        (link / "node").symlink_to(root / "bin" / "node")
        self.assertEqual(runtime_detect.node_root(), root)
        self.assertEqual(runtime_detect.node_version(), (root, "v22.3.0"))

    def test_no_node_is_a_reported_skip_not_a_failure(self):
        self.assertIsNone(runtime_detect.node_root())
        check = doctor.check_node()
        self.assertEqual(check.name, "runtime:node")
        self.assertEqual(check.status, doctor.SKIPPED)
        self.assertFalse(check.failed)

    def test_doctor_reports_version_through_the_runtime_checks(self):
        root = self.install(self.home / ".hermes" / "node", "v20.1.0")
        check = doctor.check_node()
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("v20.1.0", check.detail)
        self.assertIn(str(root), check.detail)

    def test_a_broken_node_is_not_reported_as_available(self):
        root = self.install(self.home / ".hermes" / "node", "garbage")
        self.assertEqual(runtime_detect.node_version(root)[0], None)


@unittest.skipUnless(bwrap_works(), "unprivileged bubblewrap unavailable")
class NodeInRealSandbox(unittest.TestCase):
    """#685: run through real bubblewrap; the seat finds `node` on PATH only via the mount.

    The install is a stand-in script (no real Node exists where this runs): it proves the
    bind and PATH entry, which is all the launcher controls. A real host Node needs `selftest`.
    """
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for name in ("code", "home", "export"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "q.txt").write_text("q\n")
        self.node = self.root / "node-install"
        (self.node / "bin").mkdir(parents=True)
        script = self.node / "bin" / "node"
        script.write_text('#!/bin/sh\ncase "$1" in --version) echo v22.9.9;; '
                          '--test) echo "ok $2"; echo "# pass 1";; esac\n')
        script.chmod(0o755)

    def run_in_sandbox(self, shell: str, node):
        kw = dict(code=self.root / "code", venv=Path("/usr"), runtime=Path("/usr"),
                  home=self.root / "home", checkout=self.root / "export", rust=Path("/usr"),
                  query=self.root / "q.txt", entry=["/bin/sh", "-c", shell])
        if node is not None:
            kw["node"] = node
        return contained.run(timeout=60, **kw)

    def test_seat_runs_node_version_and_node_test(self):
        done = self.run_in_sandbox("node --version && node --test /work/x.test.js", self.node)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = done.stdout.decode() if isinstance(done.stdout, bytes) else done.stdout
        self.assertIn("v22.9.9", out)
        self.assertIn("ok /work/x.test.js", out)

    def test_without_the_mount_the_seat_has_no_node(self):
        done = self.run_in_sandbox("node --version", None)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
