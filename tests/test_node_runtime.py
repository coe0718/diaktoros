"""Host Node detection and its doctor line (#685)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
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

    def test_home_or_credential_store_is_never_a_node_root(self):
        for rel in ("bin", ".local/bin", ".hermes/bin", ".config/bin"):
            with self.subTest(rel=rel):
                node = self.home / rel / "node"
                node.parent.mkdir(parents=True, exist_ok=True)
                node.write_text("#!/bin/sh\necho v22.3.0\n")
                node.chmod(0o755)
                with mock.patch.dict(os.environ, {"PATH": str(node.parent)}):
                    self.assertIsNone(runtime_detect.node_root())
                    self.assertIsNone(runtime_detect.node_version()[0])
                node.unlink()

    def test_node_tree_inside_a_dot_directory_is_still_accepted(self):
        root = self.install(self.home / ".local" / "share" / "node-v22")
        with mock.patch.dict(os.environ, {"PATH": str(root / "bin")}):
            self.assertEqual(runtime_detect.node_root(), root)

    def test_unsafe_candidate_does_not_hide_a_later_real_install(self):
        bad = self.home / "bin"
        bad.mkdir()
        (bad / "node").write_text("#!/bin/sh\n")
        (bad / "node").chmod(0o755)
        root = self.install(self.home / "opt" / "node")
        with mock.patch.dict(os.environ, {"PATH": f"{bad}:{root / 'bin'}"}):
            self.assertEqual(runtime_detect.node_root(), root)

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

    def system_install(self):
        """A /usr-style install: found on PATH, but a root the sandbox already sees."""
        root = self.install(self.home / "sysroot", "v18.19.0")
        env = mock.patch.dict(os.environ, {"PATH": str(root / "bin")})
        env.start()
        self.addCleanup(env.stop)
        vis = mock.patch.object(runtime_detect, "_NODE_VISIBLE", (root,))
        vis.start()
        self.addCleanup(vis.stop)
        return root

    def test_system_install_is_available_but_not_mounted(self):
        root = self.system_install()
        self.assertIsNone(runtime_detect.node_root())
        self.assertEqual(runtime_detect.node_version(), (root, "v18.19.0"))
        self.assertFalse(runtime_detect.node_mounted(root))

    def test_doctor_reports_system_node_as_verified(self):
        self.system_install()
        check = doctor.check_node()
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("v18.19.0", check.detail)
        self.assertIn("already visible", check.detail)

    def test_selftest_reports_system_node_as_pass(self):
        import json
        from diaktoros import selftest
        self.system_install()
        src = self.home / "src"
        (src / ".git").mkdir(parents=True)
        (src / "run_agent.py").write_text("")
        venv = self.home / "venv"
        (venv / "bin").mkdir(parents=True)
        for name in ("python", "hermes"):
            (venv / "bin" / name).write_text("")
        rust = self.home / "rust"
        (rust / "bin").mkdir(parents=True)
        (rust / "bin" / "cargo").write_text("")
        path = self.home / "runtime.json"
        path.write_text(json.dumps({"source": str(src), "venv": str(venv),
                                    "runtime": "/usr", "rust": str(rust)}))
        path.chmod(0o600)
        report = selftest.Report(out=io.StringIO())
        selftest.check_runtime(report, path)
        node = [r for r in report.results if r[1] == "runtime:node"]
        self.assertEqual(len(node), 1, report.results)
        self.assertEqual(node[0][2], selftest.PASS)
        self.assertIn("v18.19.0", node[0][3])
        self.assertIn("already visible", node[0][3])

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
