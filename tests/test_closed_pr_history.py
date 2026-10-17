"""Issue #723: failed turns on closed PRs are history, and an issue fix that stopped being
eligible before launch is cancelled, not failed.

GitHub is mocked; the ledger and the loop's watch state live in a temporary HERMES_HOME.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_issue_fix import BASE, LABEL, REPO, Base, issue  # noqa: E402

from diaktoros import cli, config, gh, ledger, observer, run_supervisor, seat_model  # noqa: E402
from diaktoros import state as state_mod, trusted_turn  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40


class ClosedPrHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        loops = home / "review-loops.d"
        loops.mkdir()
        self.raw = {"id": "widgets", "repo": "acme/widgets", "base": "main", "cap": 3,
                    "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                    "seats": {"reviewer": {"profile": "r", "route": "review"},
                              "fixer": {"profile": "f", "route": "fix"}},
                    "state_dir": str(home / "state"), "tokens": {}, "read_token": "reader",
                    "host": "http://127.0.0.1:9"}
        (loops / "widgets.json").write_text(json.dumps(self.raw))
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home),
                                           "DIAKTOROS_CONFIG_DIR": str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = config.load_id("widgets")
        self.db = home / "state" / "diaktoros-runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        self.sup._spawn = lambda: None
        for pr in (7, 8):
            self.sup.submit(f"r{pr}", "acme/widgets", pr, HEAD, "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=4, "
                        "error='retry limit (4 attempts): turn exited with status 3'")

    def listing(self, numbers, age=0):
        st = state_mod.state_for(self.loop)
        watch = st.watch()
        watch["open_prs"] = {"at": time.time() - age, "numbers": list(numbers)}
        st.watch_save(watch)

    def run_cli(self, func, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(argparse.Namespace(**kw))
        return code, out.getvalue()

    def states(self):
        with ledger.connect(self.db) as con:
            return dict(con.execute("SELECT pr,state FROM runs").fetchall())

    def test_failed_run_on_a_pr_absent_from_the_listing_is_history(self):
        self.listing([8])
        rows = run_supervisor.read_only_view(self.db, "acme/widgets", None)
        live, history, unknown = run_supervisor.split_history(rows, self.loop)
        self.assertEqual([r["pr"] for r in live], [8])
        self.assertEqual([r["pr"] for r in history], [7])
        self.assertFalse(unknown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.operations._print_ledger_runs(self.loop, None, "", limit=10)
        text = out.getvalue()
        self.assertIn("1 failed turn(s) on closed PRs, history (#7)", text)
        self.assertIn("reviewer #8", text)
        self.assertNotIn("reviewer #7", text)
        self.assertEqual(self.states(), {7: "failed", 8: "failed"})      # rows untouched

    def test_explain_view_for_a_closed_pr_says_history(self):
        self.listing([8])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.operations._print_ledger_runs(self.loop, 7, "", limit=10)
        self.assertIn("PR #7 is not open (merged or closed): history, nothing to re-arm",
                      out.getvalue())

    def test_failed_run_on_an_open_pr_is_still_rearmable(self):
        self.listing([7, 8])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.operations._print_ledger_runs(self.loop, None, "", limit=10)
        self.assertNotIn("history", out.getvalue())
        self.assertIn("re-arm: hermes dk retry --loop widgets --pr 7", out.getvalue())

    def test_no_listing_keeps_todays_behaviour_and_says_openness_is_unknown(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.operations._print_ledger_runs(self.loop, None, "", limit=10)
        text = out.getvalue()
        self.assertIn("re-arm: hermes dk retry --loop widgets --pr 7", text)
        self.assertIn("openness", text.replace("whether these PRs are still open is unknown",
                                               "openness"))
        self.assertIn("unknown", text)

    def test_retry_refuses_a_run_whose_pr_is_not_open(self):
        self.listing([8])
        with mock.patch.object(gh, "pr", return_value={"number": 7, "state": "closed"}):
            code, out = self.run_cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 2)
        self.assertIn("PR #7 is not open (merged or closed); nothing to re-arm", out)
        self.assertEqual(self.states()[7], "failed")

    def test_retry_confirms_with_a_read_when_the_listing_disagrees(self):
        self.listing([8])                       # the listing missed a PR reopened since
        with mock.patch.object(gh, "pr", return_value={"number": 7, "state": "open",
                                                       "head": {"sha": HEAD}}):
            code, out = self.run_cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.states()[7], "pending")

    def test_retry_of_an_open_pr_needs_no_read_while_the_listing_is_fresh(self):
        self.listing([7, 8])
        with mock.patch.object(gh, "pr", side_effect=AssertionError("no read")):
            code, out = self.run_cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 0, out)

    def test_retry_with_no_listing_is_unchanged(self):
        with mock.patch.object(gh, "pr", side_effect=AssertionError("no read")):
            code, out = self.run_cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 0, out)

    def test_report_counts_do_not_change(self):
        from diaktoros import stats
        before = stats.collect(self.loop, self.db, 0, False, now=1.0)
        self.listing([8])
        after = stats.collect(self.loop, self.db, 0, False, now=1.0)
        self.assertEqual(before, after)
        self.assertEqual(sum(d["states"].get("failed", 0)
                             for d in before["turns"].values()), 2)

    def test_the_sweep_records_the_complete_listing(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import watchdog
        src = Path(watchdog.__file__).read_text()
        self.assertIn('watch["open_prs"]', src)


class IneligibleBeforeLaunch(Base):
    """A queued issue fix whose labels went away is cancelled before launch (#723)."""

    def run_turn(self, live):
        sup, run_id = self.fix_row(state="launching")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
        sup.production_config = self.root / "runtime.json"
        notices = []
        with mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_profile", return_value=mock.Mock(
                    upstream="https://x/v1", key="k", model="m", api_mode="chat_completions",
                    proxy_model="", client_identity="", provider="custom", profile="coder",
                    credential_provider=lambda: None)), \
                mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=live), \
                mock.patch.object(trusted_turn, "run_turn",
                                  side_effect=AssertionError("no model may run")), \
                mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **k: notices.append((a, k))), \
                mock.patch.object(sup, "recover"), \
                mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            sup._run_production(run_id, "w")
        with sup._connect() as con:
            row = dict(con.execute("SELECT state,error FROM runs WHERE id=?",
                                   (run_id,)).fetchone())
            queued = con.execute("SELECT COUNT(*) FROM operator_notices").fetchone()[0]
            sup.notify(lambda message: notices.append(message))
            seen = run_supervisor.runs_view(con)
        return row, notices, queued, seen

    def test_relabelled_auto_offered_issue_is_cancelled_without_a_failure_notice(self):
        raw = {**self.raw, "triage": {**self.raw["triage"], "labels": ["bug", "P3", "documentation"],
                                      "auto_fix_labels": ["P3", "documentation"]}}
        self.loop = config.normalize(raw)
        row, notices, queued, seen = self.run_turn(issue(labels=("bug",)))
        self.assertEqual(row["state"], "cancelled")
        self.assertIn("neither the fix label", row["error"])
        self.assertIn("P3, documentation", row["error"])
        self.assertEqual(notices, [])           # no ❌ observer notice, no operator notice
        self.assertEqual(queued, 0)
        self.assertEqual(seen, [])              # never offered for re-arm

    def test_removed_fix_label_is_cancelled_too_and_a_later_failure_is_still_failed(self):
        row, notices, _, seen = self.run_turn(issue(labels=()))
        self.assertEqual(row["state"], "cancelled")
        self.assertIn("'agent-fix' label is no longer on the issue", row["error"])
        self.assertEqual((notices, seen), ([], []))
        self.assertTrue(issubclass(run_supervisor.IssueIneligible, ValueError))


if __name__ == "__main__":
    unittest.main()
