"""#1808 F4, #1811: ``end_lingering_workers`` ends only a process that marks itself as lingering.

Real processes name the test root on their command line, as detached workers do. A lingering
one holds the ``<ledger>.linger.<pid>`` mark ``Supervisor.linger_for_ci`` keeps (here through
the real ``workers.lingering``); the ledger's rows say nothing about which process that is.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from contextlib import closing
import fcntl
import os
from pathlib import Path
import signal
import sqlite3
import select
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from diaktoros import run_supervisor
from diaktoros.run_supervisor import workers
import worker_wait

HANG = "import time; time.sleep(60)"
# What linger_for_ci does around its wait: hold the mark (the real context manager), then wait.
LINGER = ("import sys, time; from pathlib import Path; "
          "from diaktoros.run_supervisor import workers\n"
          "with workers.lingering(Path(sys.argv[1])):\n"
          "    print('marked', flush=True); time.sleep(60)")


class EndLingeringWorkers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "runs.sqlite"

    def start(self, code, *args, named=True):
        # Named by the root on its command line, like a detached worker. Unnamed, it runs in the
        # root and is given the ledger by its relative path, so nothing on its command line says
        # which root it is under.
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            [str(Path(run_supervisor.__file__).resolve().parents[2])]
            + os.environ.get("PYTHONPATH", "").split(os.pathsep))}
        proc = subprocess.Popen([sys.executable, "-c", code, *args, *([str(self.root)] if named else [])],
                                stdout=subprocess.PIPE, text=True, env=env,
                                cwd=None if named else self.root)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def hung(self):
        return self.start(HANG)

    def lingerer(self):
        proc = self.start(LINGER, str(self.db))
        self.assertEqual(proc.stdout.readline().strip(), "marked")
        return proc

    def ledger(self, state, error=None):
        self.db.unlink(missing_ok=True)                     # a fresh ledger with one run
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE runs(id TEXT, state TEXT, error TEXT)")
            con.execute("INSERT INTO runs VALUES('r', ?, ?)", (state, error))

    def polled_waiting_row(self):
        self.ledger("waiting", f"{run_supervisor.CLAIM_READ_FAILED}: 502")

    def test_a_marked_lingering_worker_is_ended(self):
        self.polled_waiting_row()
        proc = self.lingerer()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertEqual(proc.wait(10), -signal.SIGTERM)
        worker_wait.wait_for_workers(self.root, timeout=5)

    def test_a_hung_process_beside_a_polled_waiting_row_and_a_lingerer_still_fails_the_wait(self):
        # #1811: the ledger shows a linger, and one worker is lingering; the other process under
        # the same root is stuck. Only the marked one is ended, so the exit check fails.
        self.polled_waiting_row()
        lingerer, stuck = self.lingerer(), self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertEqual(lingerer.wait(10), -signal.SIGTERM)
        self.assertIsNone(stuck.poll())                     # not terminated
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_an_unmarked_process_beside_a_polled_waiting_row_is_not_ended(self):
        # The old ledger-only judgement ended this one: the row alone does not name the process.
        self.polled_waiting_row()
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_worker_with_a_run_at_work_is_left_running_and_the_wait_still_fails(self):
        self.ledger("running")
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_marked_process_outside_the_root_is_neither_selected_nor_ended(self):
        # F5 of #1823: a valid, held mark of a live process that does not name the root. Without
        # the root intersection in lingering_workers it would be selected and killed.
        self.polled_waiting_row()
        stuck = self.hung()
        elsewhere = self.start(LINGER, self.db.name, named=False)
        self.assertEqual(elsewhere.stdout.readline().strip(), "marked")
        self.assertIn(elsewhere.pid, workers.lingering_pids(self.db))   # the mark itself is valid
        self.assertEqual(worker_wait.lingering_workers(self.root), set())
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(elsewhere.poll())
        self.assertIsNone(stuck.poll())

    def test_the_mark_of_a_process_that_is_gone_ends_nothing(self):
        stuck = self.hung()
        workers.linger_mark(self.db, 2 ** 22 + 1).write_text("4194305 7\n")   # no such process
        self.assertEqual(worker_wait.lingering_workers(self.root), set())
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())

    def test_no_ledger_and_no_mark_is_not_a_linger(self):
        stuck = self.hung()
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())

    def test_end_lingering_workers_has_no_way_to_vouch_for_a_worker(self):
        with self.assertRaises(TypeError):
            worker_wait.end_lingering_workers(self.root, settle=0.1, vouched=True)

    def test_a_worker_that_wakes_after_it_was_selected_is_not_ended_and_fails_the_wait(self):
        # #1830: selected while marked, awake before the signal, then at work (and hung). The
        # signal must not reach it, or the exit check would pass on work that never finished.
        self.polled_waiting_row()
        code = ("import os, sys, time; from pathlib import Path\n"
                "from diaktoros.run_supervisor import workers\n"
                "with workers.lingering(Path(sys.argv[1])):\n"
                "    print('marked', flush=True)\n"
                "    while not os.path.exists(sys.argv[1] + '.wake'): time.sleep(0.01)\n"
                "print('working', flush=True); time.sleep(60)")
        proc = self.start(code, str(self.db))
        self.assertEqual(proc.stdout.readline().strip(), "marked")
        real = workers.lingering_pids
        woken = []

        def selected_then_woken(db):
            picked = real(db)                           # the worker is picked while marked...
            if not woken:
                woken.append(1)
                Path(str(self.db) + ".wake").touch()    # ...and wakes before the signal
                self.assertEqual(proc.stdout.readline().strip(), "working")
            return picked

        with patch.object(workers, "lingering_pids", selected_then_woken):
            worker_wait.end_lingering_workers(self.root, settle=0)
        self.assertIsNone(proc.poll())                  # not ended: it was at work
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_signal_is_sent_only_to_a_worker_that_is_still_in_its_wait(self):
        self.polled_waiting_row()
        proc = self.lingerer()
        self.assertTrue(workers.signal_lingerer(self.db, proc.pid, signal.SIGTERM))
        self.assertEqual(proc.wait(10), -signal.SIGTERM)
        stuck = self.hung()                             # never marked: no gate, no signal
        self.assertFalse(workers.signal_lingerer(self.db, stuck.pid, signal.SIGTERM))
        self.assertIsNone(stuck.poll())

    def test_a_worker_leaving_its_wait_cannot_work_until_an_authorised_signal_lands(self):
        # #1830 F1: the signaler has checked the held mark; the worker now tries to leave. It
        # must stay in _leave (on the gate's lock) until the signal is sent, never reach work.
        code = ("import os, sys, time; from pathlib import Path\n"
                "from diaktoros.run_supervisor import workers\n"
                "with workers.lingering(Path(sys.argv[1])):\n"
                "    print('marked', flush=True)\n"
                "    while not os.path.exists(sys.argv[1] + '.wake'): time.sleep(0.01)\n"
                "    print('leaving', flush=True)\n"
                "print('working', flush=True); time.sleep(60)")
        proc = self.start(code, str(self.db))
        self.assertEqual(proc.stdout.readline().strip(), "marked")
        real, early = workers._held, []

        def held_then_worker_tries_to_leave(mark):
            held = real(mark)                           # the signaler has seen the mark held...
            Path(str(self.db) + ".wake").touch()        # ...and the worker now tries to leave
            self.assertEqual(proc.stdout.readline().strip(), "leaving")
            early.extend(select.select([proc.stdout], [], [], 1.0)[0])   # it must not get to work
            return held

        with patch.object(workers, "_held", held_then_worker_tries_to_leave):
            self.assertTrue(workers.signal_lingerer(self.db, proc.pid, signal.SIGTERM))
        self.assertEqual(early, [], "the worker reached work before the signal was delivered")
        self.assertEqual(proc.wait(10), -signal.SIGTERM)
        self.assertEqual(proc.stdout.read(), "")        # and it never printed 'working'

    def test_a_stale_mark_does_not_vouch_for_a_process_that_reused_its_pid(self):
        # F2 of #1823: the lingerer was killed and left its mark; a hung process under the same
        # root now holds that PID. Nothing holds the mark's lock, so the new one is not ended,
        # whatever the file says: "<pid> -" is what the earlier start-time marks wrote where
        # /proc was unreadable (round 2, F2), and it vouches for nobody now either.
        self.polled_waiting_row()
        stuck = self.hung()
        for text in (f"{stuck.pid} 1\n", f"{stuck.pid} -\n", ""):
            workers.linger_mark(self.db, stuck.pid).write_text(text)
            self.assertEqual(workers.lingering_pids(self.db), set())
            self.assertEqual(worker_wait.lingering_workers(self.root), set())
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_stale_mark_is_not_vouched_for_when_proc_is_unreadable(self):
        # Round 2, F2: with no /proc to say who is who the verdict is the same, because the
        # lock does not use /proc: an unlocked mark vouches for nobody.
        self.polled_waiting_row()               # the ledger the helper scans for marks
        stuck = self.hung()
        workers.linger_mark(self.db, stuck.pid).write_text(f"{stuck.pid} -\n")
        with patch.object(run_supervisor, "_pid_start_ticks", return_value=None):   # stat unreadable
            self.assertEqual(worker_wait.lingering_workers(self.root), set())
            worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(stuck.poll())

    def test_a_killed_lingerer_leaves_a_mark_that_counts_for_nothing(self):
        # The mark file survives SIGKILL; the lock does not.
        self.polled_waiting_row()
        gone = self.lingerer()
        gone.kill()
        gone.wait()
        self.assertTrue(workers.linger_mark(self.db, gone.pid).exists())
        self.assertEqual(workers.lingering_pids(self.db), set())

    def test_a_worker_at_work_after_a_failed_removal_fails_the_exit_check(self):
        # F4 of #1823 end to end: a process that left its wait but kept the file (unlink
        # denied) is at work, so it is not ended.
        code = ("import sys, time; from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "from diaktoros.run_supervisor import workers\n"
                "with patch.object(Path, 'unlink', side_effect=PermissionError('denied')):\n"
                "    with workers.lingering(Path(sys.argv[1])): pass\n"
                "print('left', flush=True); time.sleep(60)")
        self.polled_waiting_row()
        proc = self.start(code, str(self.db))
        self.assertEqual(proc.stdout.readline().strip(), "left")
        self.assertTrue(workers.linger_mark(self.db, proc.pid).exists())
        worker_wait.end_lingering_workers(self.root, settle=0.3)
        self.assertIsNone(proc.poll())
        with self.assertRaises(AssertionError):
            worker_wait.wait_for_workers(self.root, timeout=0.3)

    def test_a_signal_waits_for_the_gate_only_for_a_bounded_time(self):
        # #1830 F4: a signaler that cannot get the gate's lock gives up, without a signal.
        db = self.root / "runs.sqlite"
        proc = self.lingerer()
        gate = os.open(str(workers.linger_mark(db, proc.pid)) + ".end", os.O_RDONLY)
        self.addCleanup(os.close, gate)
        fcntl.flock(gate, fcntl.LOCK_EX)                # held by someone else
        result = []
        thread = threading.Thread(daemon=True, target=lambda: result.append(
            workers.signal_lingerer(db, proc.pid, signal.SIGTERM, wait=0.2)))
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())             # it gave up instead of waiting on
        self.assertEqual(result, [False])
        self.assertIsNone(proc.poll())
        fcntl.flock(gate, fcntl.LOCK_UN)
        self.assertTrue(workers.signal_lingerer(db, proc.pid, signal.SIGTERM))

    def test_a_signal_is_not_sent_under_a_gate_that_was_replaced_after_it_was_opened(self):
        # #1830 F4: the signaler holds the old gate while the worker, with its mark still held,
        # has a new one at the path. Its lock on the old file authorises nothing.
        db = self.root / "runs.sqlite"
        proc = self.lingerer()
        gate_path = Path(str(workers.linger_mark(db, proc.pid)) + ".end")
        real, replaced = workers.fcntl.flock, []

        def flock_after_gate_replaced(fd, how):
            if how == fcntl.LOCK_EX | fcntl.LOCK_NB and not replaced:
                replaced.append(1)
                gate_path.unlink()
                gate_path.write_text("")                # a different file at the same path
            return real(fd, how)

        with patch.object(workers.fcntl, "flock", flock_after_gate_replaced):
            self.assertFalse(workers.signal_lingerer(db, proc.pid, signal.SIGTERM))
        self.assertEqual(replaced, [1])
        self.assertIsNone(proc.poll())


class LingerMark(unittest.TestCase):
    def test_the_mark_exists_exactly_while_the_worker_lingers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            self.assertEqual(workers.lingering_pids(db), set())
            with workers.lingering(db):
                self.assertEqual(workers.lingering_pids(db), {os.getpid()})
            self.assertEqual(workers.lingering_pids(db), set())

    def test_the_mark_is_removed_when_the_linger_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            with self.assertRaises(RuntimeError), workers.lingering(db):
                raise RuntimeError("boom")
            self.assertEqual(workers.lingering_pids(db), set())

    def test_a_stale_mark_is_swept_by_the_next_lingerer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            workers.linger_mark(db, 2 ** 22 + 1).write_text("4194305 7\n")
            workers.linger_mark(db, 1).write_text("1 -\n")      # pid 1 is live but holds no lock
            with workers.lingering(db):
                self.assertEqual(workers.lingering_pids(db), {os.getpid()})
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),    # the mark, its gate
                                 [workers.linger_mark(db, os.getpid()).name,
                                  workers.linger_mark(db, os.getpid()).name + ".end"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_a_stale_gate_is_swept_with_its_stale_mark(self):
        # #1830 F2: a killed lingerer leaves its mark and its gate; the next lingerer sweeps both.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            stale = workers.linger_mark(db, 2 ** 22 + 1)
            stale.write_text("")
            (stale.parent / (stale.name + ".end")).write_text("")
            with workers.lingering(db):
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                                 [workers.linger_mark(db, os.getpid()).name,
                                  workers.linger_mark(db, os.getpid()).name + ".end"])

    def test_a_sweep_of_a_stale_mark_does_not_remove_the_gate_of_a_worker_taking_that_mark(self):
        # #1830 F3: the sweeper has removed the stale mark of PID P; a new worker with PID P
        # now starts to linger. It must not end up marked with its gate removed by the sweeper.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            sweeper, worker_pid = threading.get_ident(), 2 ** 22 + 5
            stale = workers.linger_mark(db, worker_pid)
            stale.write_text("")
            Path(str(stale) + ".end").write_text("")
            marked, release, started = threading.Event(), threading.Event(), []
            real_unlink, real_getpid = workers._unlink, os.getpid

            def worker():
                with workers.lingering(db):
                    marked.set()
                    release.wait(10)

            thread = threading.Thread(target=worker, daemon=True)

            def unlink_then_worker_starts(path):
                real_unlink(path)
                if path == stale and not started:       # the mark is gone; the gate is not yet
                    started.append(1)
                    thread.start()
                    marked.wait(0.5)                    # unfixed, it is marked by now

            def getpid():
                return real_getpid() if threading.get_ident() == sweeper else worker_pid

            with patch.object(workers, "_unlink", unlink_then_worker_starts), \
                    patch.object(workers.os, "getpid", getpid):
                with workers.lingering(db):
                    pass
            try:
                self.assertTrue(marked.wait(10))
                self.assertTrue(workers._held(stale))
                self.assertTrue(Path(str(stale) + ".end").exists())     # its gate is still there
            finally:
                release.set()
                thread.join(10)

    def test_a_mark_that_cannot_be_taken_leaves_no_gate_and_no_open_descriptor(self):
        # #1830 F2: the gate is created before the mark; when the mark fails it is undone.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            opened = []
            real = workers._open_gate

            def recording(mark):
                opened.append(real(mark))
                return opened[-1]

            with patch.object(workers, "_take_mark", return_value=None), \
                    patch.object(workers, "_open_gate", recording):
                with workers.lingering(db):
                    self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError):            # EBADF: the descriptor was closed
                os.fstat(opened[0])

    def test_a_mark_that_cannot_be_removed_does_not_stop_the_worker(self):
        # F3 of #1823: unlink denied at the sweep on entry and at the removal on exit.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            workers.linger_mark(db, 2 ** 22 + 1).write_text("4194305 7\n")
            ran = []
            with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                with workers.lingering(db):
                    ran.append(workers.lingering_pids(db))
            self.assertEqual(ran, [{os.getpid()}])

    def test_a_mark_that_cannot_be_removed_stops_vouching_when_the_wait_ends(self):
        # F4 of #1823: the unlink on leaving is denied and the worker goes on to work (reads,
        # recover()). The leftover file must not make it look like a lingerer.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                with workers.lingering(db):
                    self.assertEqual(workers.lingering_pids(db), {os.getpid()})
                self.assertTrue(workers.linger_mark(db, os.getpid()).exists())
                self.assertEqual(workers.lingering_pids(db), set())     # now at work

    def test_a_gone_ledger_directory_leaves_the_worker_unmarked_and_running(self):
        with workers.lingering(Path("/nonexistent-dir-1811/runs.sqlite")):
            pass


if __name__ == "__main__":
    unittest.main()
