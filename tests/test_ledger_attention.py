"""Issue #1364: the runs that need a person — superseded runs left out, the newest kept under
the cap with every uncertain run, and selftest's ``ledger:attention`` line counted by kind.

The ledger and the loop's watch state live in a temporary HERMES_HOME; nothing reaches GitHub.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from diaktoros import config, doctor, observer, run_supervisor, selftest  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros.doctor import cron_checks, hook_checks  # noqa: E402
from diaktoros.run_supervisor import Supervisor, views  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

HEAD = "a" * 40
REPO = "acme/widgets"
OFF = run_supervisor.ISSUE_FIX_CANCELLATIONS[0]


class LedgerAttention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        loops = home / "review-loops.d"
        loops.mkdir()
        raw = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
               "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
               "seats": {"reviewer": {"profile": "r", "route": "review"},
                         "fixer": {"profile": "f", "route": "fix"}},
               "state_dir": str(home / "state"), "tokens": {}, "read_token": "reader",
               "host": "http://127.0.0.1:9"}
        (loops / "widgets.json").write_text(json.dumps(raw))
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home),
                                           "DIAKTOROS_CONFIG_DIR": str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = config.load_id("widgets")
        self.db = home / "state" / "diaktoros-runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        self.sup._spawn = lambda: None
        self.clock = 1_000_000.0

    def run_row(self, pr, seat, state, error=None, repo=REPO):
        """A run of ``seat`` on ``pr`` in ``state``, created one second after the last."""
        self.clock += 1
        run_id = f"run{self.clock:.0f}"
        # Each run its own turn key: a ledger holds one turn per head, seat and key.
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,error,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, f"d-{run_id}", repo, pr, HEAD, seat, run_id, state, error,
                         self.clock, self.clock))
        return run_id

    def listed(self, **kw):
        return [(r["pr"], r["seat"], r["state"]) for r in self.sup.status(**kw)]

    # -- supersession --------------------------------------------------------------------------

    def test_a_newer_run_of_the_same_pr_and_seat_supersedes_a_failed_or_cancelled_one(self):
        self.run_row(7, "fixer", "failed", "conflict needs a person")
        self.run_row(9, "issue_fixer", "cancelled", OFF)
        self.run_row(7, "fixer", "done")
        self.run_row(9, "issue_fixer", "running")
        self.assertEqual(self.listed(), [])
        # read_only_view (status/explain) and the Desktop's stuck list share the rule.
        self.assertEqual(views.read_only_view(self.db, REPO, None), [])
        self.assertEqual(views.read_only_view(self.db, REPO, 7, HEAD), [])

    def test_another_seat_pr_or_repo_does_not_supersede(self):
        self.run_row(7, "fixer", "failed", "conflict needs a person")
        self.run_row(7, "reviewer", "done")
        self.run_row(8, "fixer", "done")
        self.run_row(7, "fixer", "done", repo="acme/other")
        self.assertEqual(self.listed(), [(7, "fixer", "failed")])

    def test_an_uncertain_run_is_never_superseded(self):
        self.run_row(7, "reviewer", "uncertain", "worker lost")
        self.run_row(7, "reviewer", "done")
        self.assertEqual(self.listed(), [(7, "reviewer", "uncertain")])

    # -- the cap -------------------------------------------------------------------------------

    def test_the_cap_keeps_the_newest_and_every_uncertain_run(self):
        self.run_row(1, "reviewer", "uncertain", "worker lost")
        for pr in range(100, 100 + views.RUNS_CAP + 5):
            self.run_row(pr, "reviewer", "failed", "retry limit")
        rows = self.sup.status()
        prs = [r["pr"] for r in rows]
        self.assertEqual(len(rows), views.RUNS_CAP + 1)
        self.assertEqual(prs[0], 1)                       # the old uncertain run, kept
        self.assertEqual(prs[1:], list(range(105, 100 + views.RUNS_CAP + 5)))   # the newest
        self.assertEqual(len(self.sup.attention()), views.RUNS_CAP + 6)

    # -- selftest's line, through check_ledger ----------------------------------------------------

    def check_ledger(self):
        out = io.StringIO()
        report = selftest.Report(out=out)
        fine = None                     # _from_doctor is patched out: the host checks are not read
        patches = [mock.patch.object(selftest, "ledger_path", return_value=self.db),
                   mock.patch.object(run_supervisor, "presence_marker",
                                     return_value=Path(self.tmp.name) / "presence"),
                   mock.patch.object(observer, "unusable", return_value="no observer configured")]
        for owner, name in ((doctor, "check_state_dir"), (cron_checks, "check_shim"),
                            (cron_checks, "check_cron_job"), (hook_checks, "check_gateway")):
            patches.append(mock.patch.object(owner, name, return_value=fine))
        with mock.patch.object(selftest, "_from_doctor"):
            for patch in patches:
                patch.start()
            try:
                selftest.check_ledger(report, self.loop, Path(self.tmp.name) / "runtime", None)
            finally:
                for patch in patches:
                    patch.stop()
        found = [r for r in report.results if r[1] == "ledger:attention"]
        return found[0][3] if found else None

    def test_selftest_counts_by_kind_and_leaves_superseded_runs_out(self):
        self.run_row(5, "reviewer", "uncertain", "worker lost")
        self.run_row(6, "fixer", "failed", "conflict needs a person")
        self.run_row(6, "fixer", "done")                                  # supersedes it
        self.run_row(7, "fixer", "failed", "conflict needs a person")
        self.run_row(8, "reviewer", "waiting", "held: ci")
        self.run_row(9, "issue_fixer", "cancelled", OFF)
        self.run_row(10, "issue_fixer", "cancelled", OFF)
        self.run_row(10, "issue_fixer", "pending")                         # supersedes it
        self.run_row(11, "fixer", "failed", "other loop", repo="acme/other")
        text = self.check_ledger()
        self.assertEqual(text, "4 run(s) need a person in the ledger: 1 uncertain, 1 failed, "
                               "1 waiting, 1 held by policy")

    def test_selftest_counts_an_uncertain_run_past_the_cap_and_says_the_list_is_cut(self):
        self.run_row(1, "reviewer", "uncertain", "worker lost")
        for pr in range(100, 100 + views.RUNS_CAP + 5):
            self.run_row(pr, "reviewer", "failed", "retry limit")
        text = self.check_ledger()
        total = views.RUNS_CAP + 6
        self.assertIn(f"{total} run(s) need a person in the ledger: 1 uncertain, "
                      f"{total - 1} failed", text)
        self.assertIn(f"`status` lists {views.RUNS_CAP + 1} of {total}", text)

    def test_selftest_leaves_failed_runs_on_closed_prs_out_of_the_count(self):
        self.run_row(7, "fixer", "failed", "conflict needs a person")
        self.run_row(8, "fixer", "failed", "conflict needs a person")
        st = state_mod.state_for(self.loop)
        watch = st.watch()
        watch["open_prs"] = {"at": time.time(), "numbers": [8]}
        st.watch_save(watch)
        self.assertEqual(self.check_ledger(), "1 run(s) need a person in the ledger: 1 failed "
                                              "(1 failed on closed PRs not counted)")

    def test_selftest_says_nothing_when_nothing_needs_a_person(self):
        self.run_row(7, "fixer", "failed", "conflict needs a person")
        self.run_row(7, "fixer", "done")
        self.assertIsNone(self.check_ledger())


if __name__ == "__main__":
    unittest.main()
