#!/usr/bin/env python3
"""#1994: a turn the gate could not enqueue (no runtime, say) is held, and its observer
"queued" notice is not sent: an operator following the feed would wait for a turn that
cannot run. A turn that really is enqueued still sends it."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import gate, merge_check  # noqa: E402

HEAD = "a" * 40
LOOP = {"id": "widgets", "repo": "acme/widgets", "base": "main", "unattended_fixer_push": True}


class QueuedNotice(unittest.TestCase):
    def gate(self, seat: str, **enqueue) -> list:
        sent, st = [], mock.Mock()
        st.queue_items.return_value = {}
        with mock.patch.object(gate, "enqueue_isolated", **enqueue), \
                mock.patch.object(merge_check, "mark_pending"), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            gate.block_pr_agent(LOOP, st, seat, 7, HEAD, lambda: sent.append(seat))
        return sent

    def test_a_turn_that_could_not_be_enqueued_sends_no_queued_notice(self):
        for seat in ("reviewer", "fixer"):
            with self.subTest(seat=seat):
                self.assertEqual(self.gate(seat, side_effect=OSError("no runtime")), [])

    def test_an_enqueued_turn_still_sends_it(self):
        for seat in ("reviewer", "fixer"):
            with self.subTest(seat=seat):
                self.assertEqual(self.gate(seat, return_value="enqueued"), [seat])


if __name__ == "__main__":
    unittest.main()
