"""#832: doctor's ``scripts`` pass line counts the files the check verified (four gates, not three)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import doctor  # noqa: E402


class ScriptsPassLine(unittest.TestCase):
    def check(self, names):
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                (pathlib.Path(tmp) / name).write_text("")
            with mock.patch.object(doctor, "scripts_dir", lambda: pathlib.Path(tmp)):
                return doctor.check_scripts()

    def test_pass_line_counts_every_gate_the_check_verifies(self):
        result = self.check(doctor.PLUGIN_SCRIPTS)
        gates = [n for n in doctor.PLUGIN_SCRIPTS if n.startswith("gate_")]
        self.assertEqual(len(gates), 4)
        self.assertEqual(result.status, doctor.VERIFIED)
        self.assertIn("4 gates", result.detail)
        self.assertIn(f"{len(doctor.PLUGIN_SCRIPTS)} files", result.detail)
        self.assertNotIn("three", result.detail)

    def test_missing_script_still_absent(self):
        result = self.check(doctor.PLUGIN_SCRIPTS[:-1])
        self.assertEqual(result.status, doctor.ABSENT)


if __name__ == "__main__":
    unittest.main()
