"""#481: the worker claims queued runs by class (CI-fix/conflict, fix round, review, new issue
fix) with ageing, and `status`/`explain` show that order."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from diaktoros import ledger
from diaktoros.run_supervisor import CONFLICT_KEY, Supervisor, views, claims


class ClaimPriority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"],
                              capacity={"reviewer": 1, "fixer": 1, "issue_fixer": 1,
                                        "adjudicator": 1})

    def enqueue(self, name, seat, pr, key="", age=0.0):
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(name, "o/r", pr, "h" + name, seat, turn_key=key)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET created=? WHERE delivery=?", (time.time() - age, name))

    def claimed_order(self):
        out = []
        while True:
            got = self.sup._claim()
            if got is None:
                return out
            with ledger.connect(self.db) as con:
                out.append(con.execute("SELECT delivery FROM runs WHERE id=?", (got[0],)).fetchone()[0])
                # finish it, so its seat's capacity is free for the next claim
                con.execute("UPDATE runs SET state='succeeded' WHERE id=?", (got[0],))

    def mixed(self):
        # arrival order is the reverse of priority
        self.enqueue("issue", "issue_fixer", 1, "issue-fix", age=40)
        self.enqueue("review", "reviewer", 2, age=30)
        self.enqueue("fix", "fixer", 3, age=20)
        self.enqueue("ci", "fixer", 4, "cifix:", age=10)
        self.enqueue("conflict", "fixer", 5, CONFLICT_KEY + "b" * 40, age=5)

    def test_claim_follows_classes(self):
        self.mixed()
        self.assertEqual(self.claimed_order(), ["ci", "conflict", "fix", "review", "issue"])

    def test_aged_low_class_goes_first(self):
        self.mixed()
        self.enqueue("old-issue", "issue_fixer", 9, "issue-fix", age=3 * claims.CLAIM_AGE_STEP_S + 1)
        self.assertEqual(self.claimed_order()[0], "old-issue")

    def test_status_view_shows_order_and_why(self):
        self.mixed()
        self.enqueue("old-issue", "issue_fixer", 9, "issue-fix", age=3 * claims.CLAIM_AGE_STEP_S + 1)
        view = views.pending_view(self.db, "o/r")
        self.assertEqual([r["seat"] for r in view][:2], ["issue_fixer", "fixer"])
        self.assertIn("aged up", view[0]["why"])
        self.assertIn("CI-fix", view[1]["why"])
        self.assertEqual(claims.claim_class({"seat": "reviewer", "turn_key": ""}), 2)

    def printed(self, pr):
        import contextlib
        import io
        from diaktoros.cli import operations
        out = io.StringIO()
        with mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(out):
            operations._print_ledger_runs({"id": "l", "repo": "o/r"}, pr, "", limit=10)
        return out.getvalue()

    def test_cli_status_and_explain_print_queue_order_and_why(self):
        self.mixed()
        self.enqueue("old-issue", "issue_fixer", 9, "issue-fix", age=3 * claims.CLAIM_AGE_STEP_S + 1)
        status = self.printed(None).splitlines()
        self.assertEqual(len(status), 6)
        self.assertIn("queue 1 of 6: issue_fixer #9", status[0])
        self.assertIn("aged up", status[0])
        self.assertIn("queue 2 of 6: fixer #4", status[1])
        self.assertIn("CI-fix/conflict turn", status[1])
        explain = self.printed(3)
        self.assertEqual(len(explain.splitlines()), 1)
        self.assertIn("queue 4 of 6: fixer #3", explain)
        self.assertIn("fix round", explain)


if __name__ == "__main__":
    unittest.main()
