"""#1154: a PR head whose conflict only a person can resolve is not requeued on every merge to the
base. Before, each new base tip made a new conflict turn key, so the same refusal ran again and
sent another failure notice (six in a day on #1154)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import conflict_person, gate, gh  # noqa: E402
from diaktoros import watch_conflicts  # noqa: E402
from scripts import watchdog  # noqa: E402

HEAD = fg.HEAD
TIP = "d" * 40


class Requeue(fg.Base):
    def act(self):
        with mock.patch.object(watch_conflicts, "base_tip", return_value=TIP), \
                mock.patch.object(watchdog.config, "unattended_fixer_push_enabled",
                                  return_value=True), \
                mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as enqueue, \
                mock.patch.object(gh, "reviews",              # approved: no fix owed (#1907)
                                  return_value=[fg.verdict(state="APPROVED")]), \
                mock.patch.object(watchdog.observer, "notify"):
            watch_conflicts.act_on_conflicts(self.loop, self.st, [(7, HEAD, "b" * 40, "fixer")], {})
        return enqueue

    def test_a_recorded_head_is_not_requeued_when_the_base_moves(self):
        self.act().assert_called_once()                              # nothing recorded: queued
        conflict_person.record(self.st, 7, HEAD, "main changed workflow files")
        self.act().assert_not_called()
        conflict_person.record(self.st, 7, "e" * 40, "older head")   # a new push clears it
        self.act().assert_called_once()

    def test_a_whole_file_record_lapses_but_a_workflow_one_does_not(self):
        # #1271: a later base can fix a whole-file conflict, so its record lapses and the next
        # base tip is tried again; the workflow-scope refusal holds until a new head.
        conflict_person.record(self.st, 7, HEAD, "whole-file",
                               lapse_s=conflict_person.WHOLE_FILE_RETRY_S)
        self.act().assert_not_called()                               # within the day: held
        later = conflict_person.time.time() + conflict_person.WHOLE_FILE_RETRY_S + 1
        with mock.patch.object(conflict_person.time, "time", return_value=later):
            self.act().assert_called_once()                          # lapsed: queued again
        conflict_person.record(self.st, 7, HEAD, "workflow files")
        with mock.patch.object(conflict_person.time, "time", return_value=later * 2):
            self.act().assert_not_called()                           # never lapses

    def test_a_whole_file_record_from_before_the_lapse_lapses_too(self):
        # #1271 review: a record written before ``until`` existed has only ``at``; a whole-file
        # one (safe_push's reason) must lapse from it, an old workflow one must not.
        old = {"head": HEAD, "at": 1.0,
               "reason": f"{conflict_person.WHOLE_FILE_REASON}: 1 whole-file conflict(s)"}
        self.st.kv_set(self.st.dir / conflict_person.FILE, {"7": old})
        with mock.patch.object(conflict_person.time, "time", return_value=2.0):
            self.act().assert_not_called()                           # within its day: held
        with mock.patch.object(conflict_person.time, "time", return_value=1e9):
            self.act().assert_called_once()                          # lapsed: queued again
        old["reason"] = "main changed workflow files since this PR branched"
        self.st.kv_set(self.st.dir / conflict_person.FILE, {"7": old})
        with mock.patch.object(conflict_person.time, "time", return_value=1e9):
            self.act().assert_not_called()                           # never lapses

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

    def test_an_unreadable_store_is_logged_but_a_missing_one_is_not(self):
        with mock.patch.object(conflict_person, "log") as log:
            self.assertEqual(conflict_person.get(self.st, 7, HEAD), "")   # no file yet
            log.assert_not_called()
            self.st.dir.mkdir(parents=True, exist_ok=True)
            (self.st.dir / conflict_person.FILE).write_text('{"7": {"head": ')   # truncated
            self.assertEqual(conflict_person.get(self.st, 7, HEAD), "")
            log.assert_called_once()
            self.assertIn("conflict-person.json", log.call_args.args[0])
            log.reset_mock()
            (self.st.dir / conflict_person.FILE).write_text("[1]")
            self.assertEqual(conflict_person.get(self.st, 7, HEAD), "")
            log.assert_called_once()

    def test_an_inaccessible_store_or_parent_is_logged_not_taken_for_missing(self):
        conflict_person.record(self.st, 7, HEAD, "r")
        path = self.st.dir / conflict_person.FILE
        real_read, real_exists = pathlib.Path.read_text, pathlib.Path.exists

        def denied(real):            # what an unsearchable parent raises, even when run as root
            def call(p, *a, **k):
                if p == path:
                    raise PermissionError(13, "Permission denied", str(p))
                return real(p, *a, **k)
            return call

        with mock.patch.object(conflict_person, "log") as log, \
                mock.patch.object(pathlib.Path, "read_text", denied(real_read)), \
                mock.patch.object(pathlib.Path, "exists", denied(real_exists)):
            self.assertEqual(conflict_person.get(self.st, 7, HEAD), "")
            self.assertEqual(log.call_count, 1)
            self.assertIn("PermissionError", log.call_args.args[0])
            log.reset_mock()
            conflict_person.prune(self.st, [])           # prune reads the same way
            self.assertEqual(log.call_count, 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores modes")
    def test_a_real_unsearchable_parent_is_logged(self):
        conflict_person.record(self.st, 7, HEAD, "r")
        self.st.dir.chmod(0)
        try:
            with mock.patch.object(conflict_person, "log") as log:
                self.assertEqual(conflict_person.get(self.st, 7, HEAD), "")
            log.assert_called_once()
        finally:
            self.st.dir.chmod(0o700)

    def test_the_watchdog_conflict_pass_logs_an_unreadable_store(self):
        self.st.dir.mkdir(parents=True, exist_ok=True)
        (self.st.dir / conflict_person.FILE).write_text("{")
        with mock.patch.object(conflict_person, "log") as log:
            self.act().assert_called_once()      # old behaviour kept: queued
        log.assert_called()


if __name__ == "__main__":
    unittest.main()
