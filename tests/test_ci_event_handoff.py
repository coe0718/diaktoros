"""#1996 (and review on #2577): notes left as the sweep lock goes are taken back, within bounds.

A drain takes the notes ``DRAIN_PASSES`` times; whoever lets the lock go runs ``handoff``, which
takes it back without waiting while notes are left, at most ``HANDOFFS`` times. So a note whose
handler stopped waiting just before the release is acted on at once, and a stream of suites can
not hold the lock: what is left past the bound stays noted for the next sweep. Harness:
``test_ci_event.Handler`` (real state, notes and lock; GitHub mocked).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import os
import unittest
from unittest import mock

import test_ci_event as base
from diaktoros import ci_event

HEADS = [f"{i:040x}" for i in range(1, 31)]


def load_tests(loader, standard_tests, pattern):
    """This file's own tests only (``base.Handler``'s are run by test_ci_event)."""
    names = sorted(n for n in vars(Handoff) if n.startswith("test_"))
    return unittest.TestSuite(Handoff(name) for name in names)


class Handoff(base.Handler):
    def chain(self, length):
        """Each act notes the next commit's suite, ``length`` commits in all: an act wrapper."""
        self.prs = {n: base.pr(number=n, head=h) for n, h in enumerate(HEADS[:length], start=10)}
        act = ci_event.act

        def acting(loop, st, head, numbers):
            later = HEADS.index(head) + 1
            if later < length:
                ci_event.note(st, HEADS[later], [10 + later])
            return act(loop, st, head, numbers)
        return acting

    def left(self):
        return self.st.kv_get(self.st.dir / ci_event.NUDGES, None)

    def test_a_note_left_after_the_drains_last_pass_is_taken_back_once_the_lock_goes(self):
        # #1996: the seventh commit is noted after the drain's fifth and last take.
        acting = self.chain(7)
        with mock.patch.object(ci_event, "act", side_effect=acting):
            reason, decision = self.deliver(base.suite(head=HEADS[0], numbers=(10,)))
        self.assertEqual(decision, "accepted")
        for n in range(10, 17):
            self.assertIn(f"#{n}", reason)
        self.assertEqual(self.left(), [])

    def test_a_stream_of_suites_is_bounded_and_what_is_left_stays_noted(self):
        # Review on #2577 (F1): a note per act would hold the lock for ever; now it lets go.
        acting = self.chain(len(HEADS))
        with mock.patch.object(ci_event, "act", side_effect=acting):
            reason, _ = self.deliver(base.suite(head=HEADS[0], numbers=(10,)))
        bound = ci_event.DRAIN_PASSES * (1 + ci_event.HANDOFFS)
        self.assertEqual(reason.count("CI read for"), bound)
        self.assertEqual([e["head"] for e in self.left()], [HEADS[bound]])   # durable, not lost
        fd = os.open(self.st.dir / ci_event.SWEEP_LOCK, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                  # and the lock is free

    def test_a_note_written_after_the_holders_empty_take_is_handed_off(self):
        # Review on #2577 (F2): the holder's last take is empty, a suite is noted, its handler
        # stops waiting, then the holder lets go: its handoff acts on the note.
        fd = os.open(self.st.dir / ci_event.SWEEP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                                   # the sweep, holding it
        self.assertEqual(self.deliver(None), [])                         # its drain: empty take
        with mock.patch.object(ci_event, "LOCK_WAIT_S", 0.2):
            reason, decision = self.deliver(base.suite())
        self.assertIn("when it lets the lock go (or the next sweep does)", reason)
        self.assertEqual((self.queued, decision), ([], "accepted"))
        fcntl.flock(fd, fcntl.LOCK_UN)                                   # the sweep lets go ...
        self.assertEqual(self.deliver("handoff"), ["CI read for #7"])    # ... and hands off
        self.assertEqual((len(self.queued), self.left()), (1, []))

    def test_a_handoff_while_another_holds_the_lock_leaves_the_notes_to_it(self):
        fd = os.open(self.st.dir / ci_event.SWEEP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        ci_event.note(self.st, base.HEAD, [7])
        self.assertEqual(self.deliver("handoff"), [])
        self.assertEqual(len(self.left()), 1)
        fcntl.flock(fd, fcntl.LOCK_UN)

    def test_the_watchdog_hands_off_after_it_lets_the_lock_go(self):
        import importlib.util
        path = base.Path(__file__).resolve().parents[1] / "scripts/watchdog.py"
        spec = importlib.util.spec_from_file_location("watchdog_1996", path)
        watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watchdog)
        free = []

        def handoff(_loop, st, _log):
            fd = os.open(st.dir / ci_event.SWEEP_LOCK, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
            except BlockingIOError:
                free.append(False)
            finally:
                os.close(fd)
        with mock.patch.object(watchdog, "_sweep_loop_locked", return_value=["swept"]), \
             mock.patch.object(watchdog.ci_event, "drain", return_value=[]), \
             mock.patch.object(watchdog.ci_event, "handoff", side_effect=handoff):
            self.assertEqual(watchdog.sweep_loop(base.LOOP, self.st), ["swept"])
        self.assertEqual(free, [True])


if __name__ == "__main__":
    unittest.main()
