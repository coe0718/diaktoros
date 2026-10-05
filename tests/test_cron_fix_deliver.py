"""doctor's cron remedy keeps a known --deliver target (#70)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
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

    def test_idless_job_addressed_by_name_not_question_mark(self):
        name = "watchdog-x"
        self.assertEqual(doctor._job_ref({"id": "j1", "name": "n"}, name), "j1")
        self.assertEqual(doctor._job_ref({"id": "", "name": name}, name), name)
        self.assertEqual(doctor._job_ref({}, name), name)
        self.assertNotIn("remove ?", doctor.cron_replace_fix(
            {"id": "x"}, [doctor._job_ref({"id": ""}, name)]))

    def test_repair_fix_names_runnable_command(self):
        self.assertNotIn("set/apply/uninstall", doctor.REPAIR_FIX)


if __name__ == "__main__":
    unittest.main()
