"""Seventh review on #2573 (F7): fenced code in a Requirements section is code, as Markdown
renders it, not requirement items, statuses or headings. A ``diff`` given as evidence read as
unmarked items, so a fully marked APPROVE was refused. GitHub is mocked."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

import test_issue_facts as tif
from diaktoros import gh, issue_facts

LOOP = tif.LOOP
DIFF = "Example evidence:\n```diff\n- old value\n+ new value\n```\n"


def check(body: str, verdict: str = "APPROVE") -> str:
    return issue_facts.check(LOOP, 7, verdict, "## Requirements\n- first: met\n\n" + body)


class Fences(unittest.TestCase):
    def setUp(self):
        fake = tif.Fake()
        for name, fn in (("api", fake.api), ("issue_comments_read", fake.comments)):
            patch = mock.patch.object(gh, name, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)

    def test_fenced_evidence_is_not_an_item(self):
        for body in (DIFF, DIFF.replace("```", "~~~~"), "  ```\n  - nested: code\n  ```\n",
                     "````md\n- a: x\n```\n- b: y\n````\n"):       # a shorter fence inside
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_a_real_unmarked_item_after_the_fence_is_still_refused(self):
        for fence in (DIFF, "```sh\n# a shell comment, not a heading\n```\n"):
            with self.subTest(fence):
                reason = check(fence + "- second: export flag\n")
                self.assertIn('"- second: export flag" has no status', reason)

    def test_a_status_inside_a_fence_counts_for_nothing(self):
        self.assertIn("`not met` requirement", check("- two: not met\n"))       # control
        self.assertEqual(check("```\n- two: not met\n```\n"), "")
        reason = issue_facts.check(LOOP, 7, "APPROVE", "## Requirements\n```\n- one: met\n```\n")
        self.assertIn("giving each requirement `met`", reason)     # an empty section

    def test_an_unclosed_fence_runs_to_the_end(self):
        self.assertEqual(check("```diff\n- old value\n- not met\n"), "")


if __name__ == "__main__":
    unittest.main()
