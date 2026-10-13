"""Host Node detection and its doctor line (#685)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import doctor, runtime_detect  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
