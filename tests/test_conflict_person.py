"""#1154: a PR head whose conflict only a person can resolve is not requeued on every merge to the
base. Before, each new base tip made a new conflict turn key, so the same refusal ran again and
sent another failure notice (six in a day on #1154)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import conflict_person, gate  # noqa: E402
from scripts import watchdog  # noqa: E402

HEAD = fg.HEAD
TIP = "d" * 40


class Requeue(fg.Base):
    def act(self):
        with mock.patch.object(watchdog, "base_tip", return_value=TIP), \
                mock.patch.object(watchdog.config, "unattended_fixer_push_enabled",
                                  return_value=True), \
                mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as enqueue, \
                mock.patch.object(watchdog.observer, "notify"):
            watchdog.act_on_conflicts(self.loop, self.st, [(7, HEAD, "b" * 40, "fixer")], {})
        return enqueue

    def test_a_recorded_head_is_not_requeued_when_the_base_moves(self):
        self.act().assert_called_once()                              # nothing recorded: queued
        conflict_person.record(self.st, 7, HEAD, "main changed workflow files")
        self.act().assert_not_called()
        conflict_person.record(self.st, 7, "e" * 40, "older head")   # a new push clears it
        self.act().assert_called_once()

    def test_reads_are_lock_free_and_prune_drops_closed_prs(self):
        conflict_person.record(self.st, 7, HEAD, "r")
        conflict_person.record(self.st, 8, HEAD, "r")
        with mock.patch.object(self.st, "locked", side_effect=AssertionError("took the lock")):
            self.assertEqual(conflict_person.get(self.st, 7, HEAD), "r")
            self.assertEqual(conflict_person.get(self.st, 9, HEAD), "")
        conflict_person.prune(self.st, [7])
        self.assertEqual(conflict_person.get(self.st, 8, HEAD), "")
        self.assertEqual(conflict_person.get(self.st, 7, HEAD), "r")
        self.assertEqual(conflict_person.get(mock.Mock(), 7, HEAD), "")   # unreadable: none


if __name__ == "__main__":
    unittest.main()
