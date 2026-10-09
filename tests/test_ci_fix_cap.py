"""#539: ``ci_fix_cap`` moves on every path: init, setup, set, apply, the loop file; doctor reports it."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_review_after_ci as rac  # noqa: E402
from diaktoros import cli, config, doctor  # noqa: E402


class CiFixCap(rac.Cli):
    def written(self):
        return json.loads(rac.LOOP_FILE.read_text()).get("ci_fix_cap")

    def test_init_form_and_flag(self):
        for extra, settings, want in (((), None, 3), ((), {"ci_fix_cap": 5}, 5),
                                      (("--ci-fix-cap", "2"), {"ci_fix_cap": 5}, 2)):
            with self.subTest(extra=extra, settings=settings):
                rac.LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_set_and_apply_move_it_and_refuse_out_of_range(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", rac.LOOP_ID, "--ci-fix-cap", "4")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), 4)
        rc, out = self.cli("set", "--loop", rac.LOOP_ID, "--ci-fix-cap", "11")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), 4)
        rc, out = self.cli("apply", "--loop", rac.LOOP_ID, settings={"ci_fix_cap": 6})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), 6)

    def test_setup_hands_init_the_answer(self):
        for settings, flags, want in (({"ci_fix_cap": 5}, [], "--ci-fix-cap=5"),
                                      ({}, ["--ci-fix-cap", "7"], "--ci-fix-cap=7")):
            args = t.parser_for(settings).parse_args(["setup", "--repo", t.REPO, *flags])
            argv, _ = cli._setup_init_argv(args, t.REPO, rac.LOOP_ID, interactive=False)
            self.assertIn(want, argv)

    def test_doctor_reports_it(self):
        on = config.normalize(rac.raw(fix_ci=True, unattended_fixer_push=True, ci_fix_cap=4))
        self.assertIn("ci_fix_cap=4", doctor.check_fix_ci(on).detail)
        off = config.normalize(rac.raw(ci_fix_cap=4))
        self.assertIn("ci_fix_cap=4", doctor.check_fix_ci(off).detail)


# The inherited review_after_ci / fix_ci tests run in their own modules.
for _name in [n for n in dir(rac.Cli) if n.startswith("test_") and n not in CiFixCap.__dict__]:
    setattr(CiFixCap, _name, None)

if __name__ == "__main__":
    unittest.main()
