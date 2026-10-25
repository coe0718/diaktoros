"""#1243 on the path a live reviewer's write takes: a run-scoped broker (run id, ledger and
generation), so the APPROVE goes through ``review_receipt.submit``. The shadow review of #1248
showed the IPC tests reached only ``broker.perform``; this one fails without the receipt-path
catch. And #1250: a no-write run gets the same answer a real write would."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import _ci_green  # noqa: F401  CI reads as green unless a test says otherwise
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_partial_view_no_approve as fixture  # noqa: E402

DIRTY = {"mergeable": False, "mergeable_state": "dirty"}


class ReceiptPathConflict(unittest.TestCase):
    # The fixture's methods, borrowed: inheriting its class would rerun its tests here.
    setUp = fixture.Broker.setUp
    api = fixture.Broker.api
    ledgered = fixture.Broker.ledgered
    start = fixture.Broker.start
    send = fixture.Broker.send
    receipts = fixture.Broker.receipts

    def test_a_live_approve_on_a_conflicting_pr_ends_the_turn_with_nothing_claimed(self):
        sup, scope = self.ledgered("")
        self.pr = {**self.pr, **DIRTY}
        server = self.start(scope, require_receipt=True)
        answer = self.send(server, "APPROVE")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["result"]["accepted"], answer["result"]["held"]),
                         (False, "conflict"))
        self.assertEqual(self.posts, [])                          # no review POSTed
        self.assertEqual(self.receipts(sup), [])                  # no receipt claimed
        self.assertTrue(server.completed)                         # done, never retried

    def test_a_no_write_run_gets_the_same_answer_as_a_real_write(self):
        _sup, scope = self.ledgered("")
        self.pr = {**self.pr, **DIRTY}
        server = self.start(scope, no_write=True)
        answer = self.send(server, "APPROVE")
        self.assertEqual(answer["result"]["held"], "conflict")
        [entry] = server.recorded
        self.assertFalse(entry["authorized"])
        self.assertIn("now conflicts", entry["held"])
        self.assertEqual(self.posts, [])
        clean = self.start(scope, no_write=True)
        self.pr = {**self.pr, "mergeable": True, "mergeable_state": "clean"}
        self.assertTrue(self.send(clean, "APPROVE")["ok"])
        self.assertTrue(clean.recorded[-1]["authorized"])


if __name__ == "__main__":
    unittest.main()
