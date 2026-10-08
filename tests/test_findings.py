"""Numbered review findings across rounds (#475): omitted, unchanged-code and explain behaviours."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import findings, state as state_mod  # noqa: E402

H1, H2 = "a" * 40, "b" * 40
ROUND1 = "F1: src/a.py: leaks the handle\nF2: src/b.py: no test\n"


class Findings(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.loop = {"repo": "acme/w", "id": "l", "base": "main", "state_dir": temp.name}
        self.st = state_mod.state_for(self.loop)
        findings.record(self.loop, 7, H1, ROUND1)

    def check(self, body, changed=("src/a.py",)):
        entry = self.st.findings_get(7)
        with mock.patch.object(findings, "changed_files", return_value=set(changed)):
            return findings.check(self.loop, entry, 7, H2, body)

    def test_first_round_needs_nothing_and_is_recorded_open(self):
        self.assertEqual([f for f in findings.open_ids(self.st.findings_get(7))], ["F1", "F2"])

    def test_round_that_omits_an_open_finding_is_refused(self):
        reason = self.check("F1: fixed\n")
        self.assertIn("F2", reason)
        self.assertIn("left out", reason)

    def test_round_accounting_for_every_finding_passes(self):
        self.assertEqual(self.check("F1: fixed\nF2: open\n"), "")
        self.assertEqual(self.check("F1: withdrawn\nF2: fixed\n"), "")

    def test_closed_findings_need_no_restating(self):
        findings.record(self.loop, 7, H2, "F1: fixed\nF2: withdrawn\n")
        self.assertEqual(self.check("F3: missed earlier: oops\n"), "")

    def test_new_finding_on_unchanged_code_is_refused(self):
        reason = self.check("F1: fixed\nF2: open\nF3: src/c.py: new complaint\n")
        self.assertIn("F3", reason)
        self.assertIn("missed earlier", reason)

    def test_new_finding_on_changed_code_or_marked_missed_passes(self):
        base = "F1: fixed\nF2: open\n"
        self.assertEqual(self.check(base + "F3: src/a.py: regression\n"), "")
        self.assertEqual(self.check(base + "F3: src/c.py: missed earlier: forgot it\n"), "")

    def test_unreadable_change_does_not_pass_a_cited_finding(self):
        entry = self.st.findings_get(7)
        with mock.patch.object(findings, "changed_files", return_value=None):
            reason = findings.check(self.loop, entry, 7, H2,
                                    "F1: fixed\nF2: open\nF3: src/a.py: x\n")
        self.assertIn("could not read", reason)

    def test_states_are_kept_and_listed_for_explain(self):
        findings.record(self.loop, 7, H2,
                        "F1: fixed\nF2: open\nF3: src/c.py: missed earlier: late\n")
        out = findings.lines(self.st.findings_get(7))
        self.assertEqual(len(out), 3)
        self.assertTrue(out[0].startswith("F1 fixed"))
        self.assertTrue(out[1].startswith("F2 open"))
        self.assertTrue(out[2].startswith("F3 open (missed earlier)"))
        self.assertEqual(findings.missed_count(self.loop), 1)


if __name__ == "__main__":
    unittest.main()
