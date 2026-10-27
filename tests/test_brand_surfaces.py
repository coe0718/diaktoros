"""User-facing surfaces say Diaktoros, not the pre-rename 'review loop' product name (#829).

The compatibility names (`hermes review-loop`, `review-loop-watchdog.py`, the old job name that
migrate recognises) are deliberately not covered here.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import diaktoros  # noqa: E402
from diaktoros import config, directsdk_guard, prompts  # noqa: E402
import watchdog  # noqa: E402

OLD_NAME = re.compile(r"review[- ]loops?", re.I)


class BrandSurfaces(unittest.TestCase):
    def test_module_docstrings(self):
        for module in (diaktoros, directsdk_guard):
            with self.subTest(module=module.__name__):
                self.assertNotRegex(module.__doc__, OLD_NAME)
        self.assertTrue(diaktoros.__doc__.startswith("Diaktoros"))

    def test_plugin_yaml_desktop_path_names_diaktoros(self):
        text = (ROOT / "plugin.yaml").read_text()
        self.assertIn("Plugins → Diaktoros", text)
        self.assertNotRegex(text, re.compile(r"Plugins\s*→\s*review", re.I))

    def test_adjudicator_prompts_open_with_diaktoros(self):
        for name in ("ADJUDICATOR", "ISOLATED_ADJUDICATOR"):
            with self.subTest(prompt=name):
                first = getattr(prompts, name).splitlines()[0]
                self.assertTrue(first.startswith("The Diaktoros loop for"), first)
                self.assertNotRegex(first, OLD_NAME)

    def test_watchdog_help_names_diaktoros(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["watchdog.py", "--help"]), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            watchdog.main()
        self.assertIn("Diaktoros", out.getvalue())
        self.assertNotRegex(out.getvalue(), OLD_NAME)

    def test_current_watchdog_job_is_named_diaktoros(self):
        self.assertEqual(config.WATCHDOG_JOBS["diaktoros-watchdog.py"], "diaktoros watchdog")


if __name__ == "__main__":
    unittest.main()
