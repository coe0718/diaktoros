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

    def test_non_numeric_finding_key_is_skipped_not_a_crash(self):
        entry = self.st.findings_get(7)
        entry["findings"]["F"] = {"state": "open"}
        entry["findings"]["Fx"] = {"state": "open"}
        self.assertEqual(findings.open_ids(entry), ["F1", "F2"])
        with mock.patch.object(findings, "changed_files", return_value={"src/a.py"}):
            reason = findings.check(self.loop, entry, 7, H2, "F1: fixed\nF2: fixed\n")
        self.assertEqual(reason, "")

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

    def test_approve_with_an_open_finding_is_refused(self):
        entry = self.st.findings_get(7)
        with mock.patch.object(findings, "changed_files", return_value={"src/a.py"}):
            for body in ("F1: fixed\nF2: open\n", "F1: fixed\n"):
                self.assertIn("cannot APPROVE",
                              findings.check(self.loop, entry, 7, H2, body, "APPROVE"))
            self.assertIn("cannot APPROVE", findings.check(
                self.loop, entry, 7, H2, "F1: fixed\nF2: fixed\nF3: src/a.py: new\n", "APPROVE"))
            self.assertEqual(findings.check(self.loop, entry, 7, H2,
                                            "F1: fixed\nF2: withdrawn\n", "APPROVE"), "")
            self.assertEqual(findings.check(self.loop, entry, 7, H2,
                                            "F1: fixed\nF2: open\n", "REQUEST_CHANGES"), "")

    def test_new_finding_starting_with_a_state_word_is_recorded(self):
        findings.record(self.loop, 7, H2,
                        "F1: fixed\nF2: open\nF3: open file handle leaks in x.py\n"
                        "F4: Fixed-size buffer in y.py\n")
        got = self.st.findings_get(7)["findings"]
        self.assertEqual((got["F3"]["state"], got["F4"]["state"]), ("open", "open"))
        self.assertIn("file handle", got["F3"]["text"])
        self.assertEqual(got["F1"]["state"], "fixed")
        kinds = [k for _, k, _ in findings.parse("F2: open: still\nF5: fixed\nF6: open file x")]
        self.assertEqual(kinds, ["open", "fixed", "new"])

    def test_citation_is_a_whole_path_token(self):
        base = "F1: fixed\nF2: open\n"
        self.assertIn("F3", self.check(base + "F3: data.py: broken\n"))
        self.assertIn("F3", self.check(base + "F3: src/a.pyc: broken\n"))
        self.assertEqual(self.check(base + "F3: `src/a.py`: broken\n"), "")
        self.assertEqual(self.check(base + "F3: see src/a.py, line 3\n"), "")

    def test_unreadable_change_does_not_advise_missed(self):
        with mock.patch.object(findings, "changed_files", return_value=None):
            reason = findings.check(self.loop, self.st.findings_get(7), 7, H2,
                                    "F1: fixed\nF2: open\nF3: src/a.py: x\n")
        self.assertNotIn("missed", reason)
        self.assertIn("retry", reason)

    def test_states_are_kept_and_listed_for_explain(self):
        findings.record(self.loop, 7, H2,
                        "F1: fixed\nF2: open\nF3: src/c.py: missed earlier: late\n")
        out = findings.lines(self.st.findings_get(7))
        self.assertEqual(len(out), 3)
        self.assertTrue(out[0].startswith("F1 fixed"))
        self.assertTrue(out[1].startswith("F2 open"))
        self.assertTrue(out[2].startswith("F3 open (missed earlier)"))
        self.assertEqual(findings.missed_count(self.loop), 1)

    def test_missed_count_and_stats_collect_honour_the_window(self):
        from diaktoros import stats
        first = findings.apply({}, H1, "F1: src/a.py: broken\n", at=1000.0)
        old = findings.apply(first, H2, "F2: src/c.py: missed earlier: old\n", at=1000.0)
        new = findings.apply(old, H2, "F3: src/c.py: missed earlier: new\n", at=5000.0)
        self.assertTrue(new["findings"]["F2"]["missed"] and new["findings"]["F3"]["missed"])
        self.st.findings_put(8, new)
        self.assertEqual(findings.missed_count(self.loop, 2000.0, 6000.0), 1)
        with mock.patch.object(stats, "ledger", return_value={}), \
                mock.patch.object(stats, "revisions", return_value={}):
            self.assertEqual(stats.collect(self.loop, None, 2000.0, False, now=6000.0)
                             ["findings_missed"], 1)
            self.assertEqual(stats.collect(self.loop, None, 6001.0, False, now=7000.0)
                             ["findings_missed"], 0)


class Citations(unittest.TestCase):
    """A REQUEST_CHANGES must cite the diff (#476)."""
    FILES = [{"filename": "src/a.py", "patch": "@@ -1,3 +10,4 @@\n x\n+y\n z"},
             {"filename": "docs/ops.md", "patch": "@@ -0,0 +1 @@\n+new"}]

    def check(self, body, verdict="REQUEST_CHANGES", files=FILES):
        with mock.patch("diaktoros.gh.pr_files_read", return_value=(files, "")):
            return findings.check_citations({"repo": "a/b"}, 7, verdict, body)

    def test_no_numbered_finding_is_refused(self):
        self.assertIn("no numbered blocking finding", self.check("this is bad\n"))

    def test_finding_with_no_citation_is_refused(self):
        self.assertIn("F1 does not cite", self.check("F1: it leaks\n"))

    def test_line_outside_the_diff_is_refused(self):
        self.assertIn("F1 does not cite", self.check("F1: src/a.py:200: it leaks\n"))
        self.assertIn("F1 does not cite", self.check("F1: src/other.py:11: it leaks\n"))

    def test_changed_line_or_range_passes(self):
        self.assertEqual(self.check("F1: src/a.py:11: it leaks\n"), "")
        self.assertEqual(self.check("F1: src/a.py:5-12: it leaks\n"), "")
        self.assertEqual(self.check("F1: src/a.py#L13: it leaks\n"), "")

    def test_absence_may_cite_a_changed_file(self):
        self.assertEqual(self.check("F1: src/a.py: missing test for the new branch\n"), "")
        self.assertEqual(self.check("F1: docs/ops.md: docs not updated\n"), "")
        self.assertIn("does not cite", self.check("F1: src/zzz.py: missing test\n"))
        self.assertIn("does not cite", self.check("F1: src/a.py: it leaks\n"))

    def test_approve_and_state_only_blocks_are_untouched(self):
        self.assertEqual(self.check("looks fine\n", verdict="APPROVE"), "")
        self.assertEqual(self.check("F1: open\n"), "")

    def test_unreadable_diff_is_refused(self):
        with mock.patch("diaktoros.gh.pr_files_read", return_value=(None, "HTTP 500")):
            reason = findings.check_citations({"repo": "a/b"}, 7, "REQUEST_CHANGES",
                                              "F1: src/a.py:11: x\n")
        self.assertIn("could not read", reason)


class CitationTokens(unittest.TestCase):
    """#476 on #534's path-token rule: a cited line belongs to the whole path, not a suffix."""

    def test_a_path_inside_another_name_is_not_a_citation(self):
        ranges = {"a.py": [(10, 14)]}
        self.assertFalse(findings._cited("data.py:12: wrong", ranges))
        self.assertFalse(findings._cited("lib/data.py#L12 wrong", ranges))
        self.assertTrue(findings._cited("a.py:12: wrong", ranges))
        self.assertTrue(findings._cited("see src/a.py:12 and a.py:12", ranges))
        # An absence names the changed file itself, not one whose name ends the same way.
        self.assertFalse(findings._cited("data.py: missing test", {"a.py": None}))
        self.assertTrue(findings._cited("a.py: missing test", {"a.py": None}))
        # Naming the file is not citing its line: the line must follow the whole path.
        self.assertFalse(findings._cited("a.py, compare data.py:12", ranges))

    def test_a_failed_check_name_is_a_citation(self):
        self.assertTrue(findings._cited("check tests (3.11) fails", {}, ["tests (3.11)"]))
        self.assertFalse(findings._cited("check tests (3.11) fails", {}, ["verticals"]))


if __name__ == "__main__":
    unittest.main()
