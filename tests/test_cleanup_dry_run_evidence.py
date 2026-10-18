"""#801: ``cleanup --dry-run`` skips evidence-named paths exactly as a real run does."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("cleanup_script", ROOT / "scripts" / "cleanup.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class DryRunEvidence(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.scratch = base / "scratch"
        self.scratch.mkdir()
        self.loop = {"id": "ev", "repo": "acme/widgets", "base": "main",
                     "roots": [str(self.scratch)], "state_dir": str(base / "state")}
        self.plain = self.scratch / "widgets-pr7-target"
        self.evidence = self.scratch / "widgets-pr7-evidence"
        for d in (self.plain, self.evidence):
            d.mkdir()
            (d / "blob").write_bytes(b"x" * 4096)

    def run_clean(self, dry):
        out = []
        with mock.patch.object(cleanup, "pr_state", return_value={"state": "closed"}), \
                mock.patch.object(cleanup, "worktrees", return_value=[]), \
                mock.patch.object(cleanup, "log", lambda m, quiet=False: out.append(m)):
            freed = cleanup.clean_pr(self.loop, 7, dry, False)
        return freed, out

    def test_dry_run_matches_real_run(self):
        dry_freed, dry_out = self.run_clean(True)
        self.assertTrue(self.evidence.exists())
        self.assertTrue(self.plain.exists())
        self.assertTrue(any("SKIP (evidence pattern)" in l and str(self.evidence) in l
                            for l in dry_out), dry_out)
        self.assertFalse(any("would remove" in l and str(self.evidence) in l
                             for l in dry_out), dry_out)
        real_freed, _ = self.run_clean(False)
        self.assertTrue(self.evidence.exists())
        self.assertFalse(self.plain.exists())
        self.assertGreater(real_freed, 0)
        self.assertEqual(dry_freed, real_freed)


if __name__ == "__main__":
    unittest.main()
