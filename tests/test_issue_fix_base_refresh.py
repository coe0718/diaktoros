"""An issue fix queued at an old base commit stages at the base's current tip (#1379).

Drives the real ``_run_production`` for an issue-fix row, outside world faked (as
``test_issue_fix.WorkerLaunch``): the row queued at ``BASE`` must launch, and be recorded in
the ledger, at the tip the base branch has when the run is claimed. It never launches at the
queued base (#2722 review F1): an unreadable tip waits for a retry, and a tip another run of the
issue already holds cancels this one before launch. A redelivery of the original handoff stays
the same run once its head moved (#2722 review F2).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_issue_fix as base  # noqa: E402
from diaktoros import config, gh, seat_model, trusted_turn, watch_conflicts  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

TIP = "d" * 40
DELIVERY = f"{base.REPO}:12:{base.BASE}:issue_fixer:issue-fix"


class StagesAtCurrentBase(base.Base):
    def launch(self, tip, before=None):
        sup, run_id = self.fix_row(state="launching")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
        if before is not None:
            before(sup)
        launched = []
        inference = mock.Mock(upstream="https://api.example/v1/chat/completions", key="k",
                              model="m", api_mode="chat_completions", proxy_model="",
                              client_identity="", provider="custom", profile="coder")
        inference.credential_provider.return_value = None

        def run_turn(loop, scope, **kw):
            launched.append((scope.head, kw.get("prompt") or ""))
            return 0
        sup.production_config = self.root / "runtime.json"
        with mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_profile", return_value=inference), \
                mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=base.issue()), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(watch_conflicts, "base_tip", return_value=tip), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
                mock.patch.object(sup, "recover"), \
                mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            sup._run_production(run_id, "w")
        with sup._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return sup, launched, row

    def test_a_row_queued_at_an_old_base_stages_at_the_current_one(self):
        _sup, launched, row = self.launch(TIP)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][0], TIP)          # the scope, so staging and the push
        self.assertIn(TIP, launched[0][1])             # the prompt names the head used
        self.assertEqual(row["head"], TIP)             # the ledger records the head used
        self.assertEqual(row["queued_head"], base.BASE)

    def test_an_unreadable_tip_never_launches_at_the_queued_base(self):
        # F1: no turn at the stale base; the run waits for a retry, its head unchanged.
        _sup, launched, row = self.launch(None)
        self.assertEqual(launched, [])
        self.assertEqual(row["state"], "waiting", dict(row))
        self.assertIn("tip unreadable", row["error"])
        self.assertEqual(row["head"], base.BASE)

    def test_a_tip_another_run_holds_cancels_this_one_before_launch(self):
        # F1: a second row of the issue and turn key already at the tip.
        def other_at_tip(sup):
            sup.enqueue(f"{base.REPO}:12:{TIP}:issue_fixer:issue-fix", base.REPO, 12, TIP,
                        "issue_fixer", turn_key="issue-fix")
        _sup, launched, row = self.launch(TIP, before=other_at_tip)
        self.assertEqual(launched, [])
        self.assertEqual(row["state"], "cancelled", dict(row))
        self.assertIn("superseded", row["error"])
        self.assertEqual(row["head"], base.BASE)

    def test_a_redelivery_of_the_queued_handoff_is_the_same_run(self):
        # F2: the original delivery, at the queued head, after the head moved to the tip.
        sup, launched, row = self.launch(TIP)
        self.assertEqual(row["head"], TIP)
        outcome = sup.submit(DELIVERY, base.REPO, 12, base.BASE, "issue_fixer",
                             turn_key="issue-fix")
        self.assertTrue(outcome.startswith("duplicate"), outcome)
        with sup._connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
