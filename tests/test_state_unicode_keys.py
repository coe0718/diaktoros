"""#2303 (fourth review on #2589): a key ``str.isdigit`` takes but ``int`` refuses ('²') reads as
absent beside the valid entries, in ``findings.json`` (PR numbers and finding ids) and in the
other number-keyed state files, so the real callers neither raise nor lose the valid entries.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from diaktoros import findings, history, state, stats
import _state_walk as walk

NOW = time.time()


class UnicodeDigitKeys(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.st = state.LoopState(walk.new_loop(self.root))
        self.st.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, data) -> None:
        (self.st.dir / name).write_text(json.dumps(data))

    def test_findings_keep_the_valid_pr_and_finding_beside_unreadable_ids(self):
        self.write("findings.json", {
            "²": {"findings": {}},
            "7": {"findings": {"F1": {"state": "open", "text": "a.py:1: kept", "at": NOW},
                               "F²": {"state": "open", "text": "b.py:2: odd id", "at": NOW}}}})
        tracked = self.st.findings_all()
        self.assertEqual(list(tracked), ["7"])
        db = self.root / "ledger.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE runs (repo, pr, head, seat, model, created)")
        got = stats.finding_outcomes(db, self.st.loop["repo"], 0, NOW + 10, tracked)
        self.assertEqual(got["raised"], 2)
        lines = findings.lines(tracked["7"])
        self.assertEqual([line.split()[0] for line in lines], ["F²", "F1"])
        self.assertEqual(sorted(tracked["7"]["findings"], key=history._fnum), ["F²", "F1"])

    def test_number_keyed_files_keep_the_valid_entry_beside_a_superscript_key(self):
        sha = "a" * 40
        for name, data, read, kept in (
                ("held-prs.json", {"²": {}, "3": {}}, self.st.pr_holds, {3: {}}),
                ("held-scope.json", {"²": {}, "3": {}}, self.st.scope_holds, {3: {}}),
                ("held-fixes.json", {"²": 4, "3": 4}, self.st.fix_holds, {3: 4}),
                ("late-triage.json", {"²": {"at": 1}, "3": {"at": 1}}, self.st.late_triage,
                 {3: {"at": 1.0, "outcome": "None"}}),
                ("issue-deps.json", {"²": {"1": {}}, "3": {"²": {}, "1": {}}},
                 self.st.issue_deps, {3: {1: {}}}),
                (self.st.OVERRIDE_CHECKED_FILE, {"²": sha, "3": sha}, self.st.override_checked,
                 {"3": sha})):
            with self.subTest(name):
                self.write(name, data)
                self.assertEqual(read(), kept)


if __name__ == "__main__":
    unittest.main()
