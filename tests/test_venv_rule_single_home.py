"""Detection picks a venv by the same file rule doctor validates with (#926)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import runtime_detect  # noqa: E402


class VenvRuleSingleHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"PATH": "/nonexistent"})
        env.start()
        self.addCleanup(env.stop)
        prefix = mock.patch.object(sys, "prefix", str(self.root / "no-such-prefix"))
        prefix.start()
        self.addCleanup(prefix.stop)

    def make_venv(self, venv: Path) -> Path:
        (venv / "bin").mkdir(parents=True)
        for name in ("python", "hermes"):
            (venv / "bin" / name).write_text("")
        return venv

    def test_complete_venv_is_selected_and_incomplete_is_not(self):
        source = self.root / "src"
        venv = self.make_venv(source / "venv")
        self.assertEqual(runtime_detect._venv(source), venv)
        (venv / "bin" / "hermes").unlink()
        self.assertIsNone(runtime_detect._venv(source))

    def test_detection_follows_venv_missing(self):
        source = self.root / "src"
        venv = self.make_venv(source / "venv")
        with mock.patch.object(runtime_detect, "venv_missing", return_value=["bin/extra"]):
            self.assertFalse(runtime_detect._is_hermes_venv(venv))
            self.assertIsNone(runtime_detect._venv(source))
        with mock.patch.object(runtime_detect, "venv_missing", return_value=[]):
            self.assertTrue(runtime_detect._is_hermes_venv(self.root / "empty"))


if __name__ == "__main__":
    unittest.main()
