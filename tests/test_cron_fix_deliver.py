"""doctor's cron remedy keeps a known --deliver target (#70)."""
import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from review_loop import doctor


class CronFixDeliver(unittest.TestCase):
    def test_default_local(self):
        self.assertIn("--deliver local", doctor.cron_fix({"id": "x"}))

    def test_replace_keeps_target(self):
        out = doctor.cron_replace_fix({"id": "x"}, ["j1"], "telegram")
        self.assertIn("`hermes cron remove j1`", out)
        self.assertIn("--deliver telegram", out)
        self.assertNotIn("--deliver local", out)


if __name__ == "__main__":
    unittest.main()
