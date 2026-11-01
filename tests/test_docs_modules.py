"""#1386: the docs harness's `python -m diaktoros.<module>` extractor must see the `-P` form.

The skill tells the seat to run `python -P -m diaktoros.broker_client`. An extractor that skips
the flag leaves those lines unchecked while `run_tests.py docs` stays green, so this drives the
harness's own extractor (the one `group_docs` calls) rather than a copy of its regex.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import docs  # noqa: E402


class DocsModuleExtractor(unittest.TestCase):
    def test_a_nonexistent_module_behind_dash_p_is_extracted(self):
        self.assertEqual(docs._modules("python -P -m diaktoros.no_such_module push --files a"),
                         ["no_such_module"])

    def test_the_unflagged_form_is_still_extracted(self):
        self.assertEqual(docs._modules("python -m diaktoros.run_supervisor\npython  -m diaktoros.cli"),
                         ["cli", "run_supervisor"])

    def test_other_packages_and_a_bare_package_are_not(self):
        self.assertEqual(docs._modules("python -P -m other.thing; python -m diaktoros"), [])

    def test_the_skills_dash_p_lines_are_all_extracted(self):
        text = (docs.ROOT / "skill" / "SKILL.md").read_text()
        self.assertEqual(docs._modules(text), ["broker_client"])


if __name__ == "__main__":
    unittest.main()
