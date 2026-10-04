"""Harness self-check: ``fixture.run`` names an empty-output gate's exit status and stderr,
and a gate that overruns its timeout, instead of returning an unexplained empty string."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import fixture  # noqa: E402


class RunDiagnosticsTest(unittest.TestCase):
    def run_stub(self, source: str, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "stub_gate.py").write_text(source)
            with mock.patch.object(fixture, "ROOT", root):
                return fixture.run("stub_gate.py", **kw)

    def test_empty_output_reports_exit_status_and_stderr(self):
        kind, out, err = self.run_stub(
            "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
        self.assertIn("stub_gate.py produced no output", kind)
        self.assertIn("exit 3", kind)
        self.assertIn("stderr: boom", kind)
        self.assertEqual((out, err), ("", "boom"))

    def test_timeout_reports_script_and_limit(self):
        kind, out, _ = self.run_stub("import time\ntime.sleep(30)\n", timeout=1)
        self.assertIn("stub_gate.py timed out after 1s", kind)
        self.assertEqual(out, "")

    def test_normal_output_is_untouched(self):
        kind, out, _ = self.run_stub("print('hi')\n")
        self.assertEqual((kind, out), ("hi", "hi"))


if __name__ == "__main__":
    unittest.main()
