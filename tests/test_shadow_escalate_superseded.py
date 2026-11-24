"""#2601, #2623: a shadow escalation's ruling whose shadow review is missing, not unreadable.

The review a ruling answers is the active reviewer turn's (#1683). A newer reviewer turn at the
same head (``review --reconsider`` queues one) leaves the ledger readable but with no review for
that turn: no read brings one, so the launch is no longer current instead of being retried as
"shadow review unreadable" until the retry limit. An unreadable ledger still waits (#1145).
Driven through the real launch (``_prepare_launch``) on a real run ledger.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import turn_prompts, views  # noqa: E402

ROW = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD}


class Superseded(ser.Base):
    claim = ser.Between.claim          # the real launch; not a subclass, so its tests run once

    def reviewer(self, key, at):
        """A reviewer run of turn ``key`` at the head, created at ``at``."""
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,'reviewer','succeeded',0,?,?,?)",
                        (f"rev-{key}", f"drev-{key}", fg.REPO, 7, fg.HEAD, key, at, at))

    def superseded(self):
        """A shadow review of turn t1, then a newer reviewer turn with no review of its own."""
        now = time.time()
        self.reviewer("t1", now - 200)
        self.shadow("REQUEST_CHANGES", body="F1: the first review", at=now - 100)
        self.reviewer("reconsider:1", now - 50)

    def test_the_helper_tells_a_missing_review_from_an_unreadable_ledger(self):
        self.superseded()
        self.assertIs(turn_prompts.newest_shadow_review(ROW, missing=False), False)
        self.assertIsNone(turn_prompts.newest_shadow_review(ROW))       # the default is unchanged
        with mock.patch.object(views, "read_only_connect", return_value=None):
            self.assertIsNone(turn_prompts.newest_shadow_review(ROW, missing=False))

    def test_a_superseded_shadow_review_is_no_longer_current_not_unreadable(self):
        self.superseded()
        with self.assertRaises(ValueError) as caught:
            self.claim()
        self.assertEqual(str(caught.exception), "no shadow review for the active reviewer turn")
        self.assertFalse(run_supervisor.retryable(caught.exception))
        # ...and nothing was ruled on: the breach marker was never claimed for a ruling.
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_an_unreadable_ledger_at_launch_still_waits_for_a_read(self):
        self.superseded()
        with mock.patch.object(views, "read_only_connect", return_value=None):
            with self.assertRaises(run_supervisor.RetryableError) as caught:
                self.claim()
        self.assertEqual(str(caught.exception), "shadow review unreadable before launch")

    def test_the_active_turns_own_review_launches_as_before(self):
        self.superseded()
        self.shadow("REQUEST_CHANGES", body="F2: the new turn's review", at=time.time())
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET turn_key='reconsider:1' WHERE id=?", (f"s{self.n}",))
        launch, _row = self.claim()
        self.assertIn("F2: the new turn's review", launch.prompt)
        self.assertNotIn("F1: the first review", launch.prompt)
        self.assertEqual(getattr(launch.scope, "shadow_run", None), f"s{self.n}")


if __name__ == "__main__":
    unittest.main()
