"""#2303 (fourth and fifth reviews on #2589): a number key ``int`` cannot read reads as absent
beside the valid entries, in ``findings.json`` (PR numbers and finding ids) and the other
number-keyed state files, and in each caller that turns such a key into a number, so none raises
and none loses a valid neighbour. Two such keys: '²' (``isdigit`` takes it, ``int`` refuses it)
and a run of digits past ``int``'s 4,300-digit conversion limit.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from diaktoros import desktop_api, escalate_pending, findings, fix_declined, history, state, stats
import _state_walk as walk

NOW = time.time()
BAD = ("²", "9" * 4301)


def finding(text: str) -> dict:
    return {"state": "open", "text": text, "at": NOW}


class NumberKeys(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.st = state.LoopState(walk.new_loop(self.root))
        self.st.dir.mkdir(parents=True, exist_ok=True)
        self.repo = self.st.loop["repo"]
        self.db = self.root / "ledger.db"
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            con.execute("CREATE TABLE runs (id, repo, pr, head, seat, model, created)")
            con.commit()

    def write(self, name: str, data) -> None:
        (self.st.dir / name).write_text(json.dumps(data))

    def test_findings_keep_the_valid_pr_beside_unreadable_ones(self):
        self.write("findings.json", {**{bad: {"findings": {}} for bad in BAD},
                                     "7": {"findings": {"F1": finding("a.py:1: kept")}}})
        self.assertEqual(list(self.st.findings_all()), ["7"])

    def test_finding_outcomes_skip_an_unreadable_pr_key(self):
        tracked = {**{bad: {"findings": {"F1": finding("x")}} for bad in BAD},
                   "7": {"findings": {"F1": finding("a.py:1: kept")}}}
        got = stats.finding_outcomes(self.db, self.repo, 0, NOW + 10, tracked)
        self.assertEqual(got["raised"], 1)

    def test_escalations_skip_an_unreadable_marker_key(self):
        marker = {"head": "h", "at": "2026-01-01T00:00:00Z"}
        breaches = {**{f"{self.repo}#{bad}": marker for bad in BAD}, f"{self.repo}#7": marker}
        got = stats.escalations(self.db, self.repo, 0, NOW + 10, breaches)
        self.assertEqual((got["total"], got["awaiting"]), (1, 1))

    def test_finding_ids_past_reading_sort_first_and_are_kept(self):
        entry = {"findings": {"F2": finding("b.py:2: two"), "F1": finding("a.py:1: one"),
                              **{f"F{bad}": finding("odd id") for bad in BAD}}}
        self.assertEqual([line.split()[0] for line in findings.lines(entry)],
                         [*(f"F{bad}" for bad in BAD), "F1", "F2"])
        self.assertEqual([history._fnum(f"F{bad}") for bad in BAD], [0, 0])
        self.st.findings_put(7, entry)
        self.assertEqual([item["id"] for item in desktop_api._findings(self.st.loop, 7)],
                         [*(f"F{bad}" for bad in BAD), "F1", "F2"])

    def test_number_keyed_files_keep_the_valid_entry_beside_unreadable_keys(self):
        sha, at = "a" * 40, {"at": 1}
        for name, value, read, kept in (
                ("held-prs.json", {}, self.st.pr_holds, {3: {}}),
                ("held-scope.json", {}, self.st.scope_holds, {3: {}}),
                ("held-fixes.json", 4, self.st.fix_holds, {3: 4}),
                ("late-triage.json", at, self.st.late_triage, {3: {"at": 1.0, "outcome": "None"}}),
                ("issue-deps.json", {"1": {}}, self.st.issue_deps, {3: {1: {}}}),
                (self.st.OVERRIDE_CHECKED_FILE, sha, self.st.override_checked, {"3": sha}),
                ("fix-asks.json", None, self.st.fix_ask_open, {3: {"kind": "k"}}),
                (fix_declined.FILE, {"reason": "r"}, lambda: fix_declined.entries(self.st),
                 {3: {"reason": "r", "at": 0.0, "label": fix_declined.UNKNOWN}})):
            with self.subTest(name):
                data = {**{bad: value for bad in BAD}, "3": value}
                if name == "issue-deps.json":
                    data["3"] = {**{bad: {} for bad in BAD}, "1": {}}
                if name == "fix-asks.json":
                    data = {"open": {**{bad: {"kind": "k"} for bad in BAD}, "3": {"kind": "k"}}}
                self.write(name, data)
                self.assertEqual(read(), kept)

    def test_a_dropped_escalation_request_names_only_readable_numbers(self):
        self.write(escalate_pending.FILE, {**{bad: {"at": NOW} for bad in BAD}, "5": {"at": NOW}})
        self.assertEqual(escalate_pending.prune(self.st, []), [5])


if __name__ == "__main__":
    unittest.main()
