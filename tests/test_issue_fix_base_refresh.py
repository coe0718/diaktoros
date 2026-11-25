"""An issue fix queued at an old base commit stages at the base's current tip (#1379).

Drives the real ``_run_production`` for an issue-fix row, outside world faked (as
``test_issue_fix.WorkerLaunch``): the row queued at ``BASE`` must launch, and be recorded in
the ledger, at the tip the base branch has when the run is claimed.
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


class StagesAtCurrentBase(base.Base):
    def launch(self, tip):
        sup, run_id = self.fix_row(state="launching")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
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
            head = con.execute("SELECT head FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        return launched, head

    def test_a_row_queued_at_an_old_base_stages_at_the_current_one(self):
        launched, head = self.launch(TIP)
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][0], TIP)          # the scope, so staging and the push
        self.assertIn(TIP, launched[0][1])             # the prompt names the head used
        self.assertEqual(head, TIP)                    # the ledger records the head used

    def test_an_unreadable_tip_keeps_the_recorded_base(self):
        launched, head = self.launch(None)
        self.assertEqual([h for h, _ in launched], [base.BASE])
        self.assertEqual(head, base.BASE)


if __name__ == "__main__":
    unittest.main()
