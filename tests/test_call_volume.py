"""#2609: about 30 PRs held for CI made workers start each other (9,200 in an hour) and used up
the read App's GitHub limit. One worker per ledger now polls the held rows, hands the polling over
when it stops with rows still due, and a row that cannot start now costs no GitHub read.

Real SQLite ledger; workers run in-process against a clock that moves only when one sleeps, and
GitHub is a patched ``gh.api`` that counts its calls. No network, no Hermes.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import collections
import time
import unittest
from unittest.mock import Mock, patch

import test_retryable_turns as retryable
from diaktoros import run_supervisor
from diaktoros.state import ledger

HEAD = retryable.HEAD
LOOP = {'repo': 'o/r', 'base': 'main', 'read_token': 'read'}
HELD = run_supervisor.CI_HOLD + ' on ' + HEAD[:7] + ' — checks still running'


class Volume(retryable.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**LOOP, 'state_dir': str(self.root / 'state')}
        self.s = self.sup(production_claim=True)
        self.now = [time.time()]
        clock = patch('time.time', lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    def hold(self, delivery, pr, due_in=run_supervisor.CI_POLL_S, sup=None):
        """A reviewer row held for CI, due ``due_in`` from now."""
        (sup or self.s).submit(delivery, 'o/r', pr, HEAD, 'reviewer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=? WHERE delivery=?",
                        (self.now[0] + due_in, HELD, delivery))

    def sleeper(self, sleeps=None):
        def sleep(seconds):
            if sleeps is not None:
                sleeps.append(seconds)
            self.now[0] += seconds
        return sleep

    def rehold_due(self):
        """What a poll finds while CI still runs: each due row is held again, CI_POLL_S on."""
        with ledger.connect(self.db) as con:
            return con.execute("UPDATE runs SET retry_at=? WHERE state='waiting' AND retry_at<=?",
                               (self.now[0] + run_supervisor.CI_POLL_S, self.now[0])).rowcount


class OnePoller(Volume):
    def test_a_second_lingerer_leaves_the_polling_to_the_first(self):
        from diaktoros.run_supervisor import poller
        self.hold('one', 1)
        self.s.held_for_ci = True
        first = poller.lock(self.db)                    # another worker is polling
        self.addCleanup(first.close)
        self.s.recover = Mock()
        self.s.linger_for_ci(sleep=lambda seconds: self.fail('a second poller waited'))
        self.s.recover.assert_not_called()
        first.close()                                   # that poller is gone: this one polls
        self.s.linger_for_ci(sleep=self.sleeper())
        self.s.recover.assert_called()
        again = poller.lock(self.db)                    # and it let the lock go on its way out
        self.assertNotIn(again, (None, False))
        again.close()

    def test_a_worker_takes_the_polling_up_when_rows_are_due_and_nobody_polls(self):
        # Review of #2612, F1: a row another worker held must not wait for the watchdog sweep
        # because that worker left the polling to a poller that has since gone.
        self.hold('one', 1, due_in=10)
        self.s.recover = Mock(side_effect=lambda: self.rehold_due() and None)
        self.assertFalse(self.s.held_for_ci)            # this worker held nothing itself
        self.s.linger_for_ci(sleep=self.sleeper())
        self.s.recover.assert_called()

    def test_a_poller_that_runs_out_hands_the_polling_over(self):
        # Review of #2612, F1, the reviewer's interleaving: each poll starts a worker that holds
        # the row again for 90 s while this poller still holds the lock, so that worker leaves.
        # The poller then stops at its cap with the row still due: it must start a successor.
        self.hold('one', 1, due_in=0)
        left, spawned = [], []

        def poll():
            self.rehold_due()
            other = self.sup(production_claim=True)
            other.held_for_ci = True
            other.recover = Mock()
            other.linger_for_ci(sleep=lambda seconds: self.fail('the second worker polled'))
            left.append(other.recover.called)
        self.s.recover = Mock(side_effect=poll)
        self.s._spawn = lambda: spawned.append(self.now[0])
        self.s.held_for_ci = True
        with patch.object(run_supervisor, 'POLLER_MAX_S', 100.0):
            self.s.linger_for_ci(sleep=self.sleeper())
        self.assertTrue(left and not any(left))         # the other workers left it to the poller
        self.assertEqual(len(spawned), 1)               # the successor
        successor = self.sup(production_claim=True)
        successor.recover = Mock(side_effect=self.rehold_due)
        successor.linger_for_ci(sleep=self.sleeper())   # it takes the lock and polls the row
        successor.recover.assert_called()

    def test_a_poller_that_found_nothing_due_hands_nothing_over(self):
        self.hold('one', 1, due_in=10 * 60)             # a backoff past the window: the sweep's
        spawned = []
        self.s._spawn = lambda: spawned.append(1)
        self.s.held_for_ci = True
        self.s.linger_for_ci(sleep=self.sleeper())
        self.assertEqual(spawned, [])

    def test_a_due_row_recover_cannot_release_starts_no_successor(self):
        # A poller that stops because recover() left the due row where it was must not hand it
        # over: each successor would stop the same way and start the next, without end.
        self.hold('stuck', 1, due_in=0)
        spawned = []
        self.s.recover = Mock()                         # releases nothing
        self.s._spawn = lambda: spawned.append(1)
        self.s.held_for_ci = True
        self.s.linger_for_ci(sleep=self.sleeper())
        self.s.recover.assert_called_once()
        self.assertEqual(spawned, [])

    def test_a_row_held_meanwhile_and_due_sooner_is_not_left_late(self):
        # Review of #2612: the poller waits for a row due in 90 s; another worker holds a row due
        # in 5 s meanwhile. The poller sleeps in slices, so it is seen within one.
        self.hold('late', 1, due_in=90)
        start = self.now[0]
        recovered = []
        self.s.recover = Mock(side_effect=lambda: recovered.append(self.now[0] - start)
                              or self.rehold_due())
        first = [True]

        def sleep(seconds):
            if first[0]:
                first[0] = False
                self.hold('soon', 2, due_in=5)
            self.now[0] += seconds
        self.s.held_for_ci = True
        with patch.object(run_supervisor, 'POLLER_MAX_S', 200.0):
            self.s.linger_for_ci(sleep=sleep)
        self.assertLessEqual(recovered[0], 5 + run_supervisor.POLL_SLICE_S + 1)


class NoReadForARowThatCannotStart(Volume):
    def claim_with(self, api):
        with patch('diaktoros.config.by_repo', return_value=self.loop), \
             patch('diaktoros.gh.api', api), patch('diaktoros.gh.reviews', return_value=[]):
            return self.s._claim()

    def test_a_full_seat_or_a_busy_pr_costs_no_github_read(self):
        self.s.submit('waiting-1', 'o/r', 1, HEAD, 'reviewer')
        for case, (pr, seat) in (('seat full', (2, 'reviewer')), ('PR busy', (1, 'fixer'))):
            with self.subTest(case=case):
                with ledger.connect(self.db) as con:
                    con.execute("DELETE FROM runs WHERE delivery='busy'")
                self.s.submit('busy', 'o/r', pr, HEAD, seat)
                with ledger.connect(self.db) as con:
                    con.execute("UPDATE runs SET state='running' WHERE delivery='busy'")
                api = Mock(return_value=None)
                self.assertIsNone(self.claim_with(api))
                api.assert_not_called()
                row = self.s.get('waiting-1')
                self.assertEqual((row['state'], row['retries']), ('pending', 0))
        with ledger.connect(self.db) as con:                     # the slot frees: it is read
            con.execute("UPDATE runs SET state='succeeded' WHERE delivery='busy'")
        api = Mock(return_value=None)
        self.assertIsNone(self.claim_with(api))
        api.assert_called()


class _Done(Exception):
    """The simulated span is over."""


class ThirtyHeldPRs(Volume):
    """The 2026-11-22 workload, reproduced: 30 reviewer turns held for CI (due 3 s apart), the
    reviewer seat busy with another turn (capacity 1), CI still running for 10 minutes. Every
    worker a poll starts runs at once, while the worker that started it still lingers, which
    is how they overlapped. GitHub reads are counted by path shape, as ``gh_calls`` counts
    them: each held row's poll is the PR plus its check runs and statuses.

    With the code before #2609 this workload starts a worker for nearly every poll and each of
    them polls again (the counts are in the PR); the bounds below are what it does now. The
    decisions are the same: no turn starts while the seat is busy, none fails or is cancelled,
    and once CI is green and the seat frees, every one of the 30 is reviewed."""
    SPAN = 600.0
    MAX_WORKERS = 400           # the old code passes this long before the span ends

    def setUp(self):
        super().setUp()
        self.calls = collections.Counter()
        self.workers = 0
        self.ci_running = True
        for n in range(1, 31):
            self.hold(f'pr{n}', n, due_in=3 * n)
        self.s.submit('busy', 'o/r', 99, HEAD, 'reviewer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running', lease=? WHERE delivery='busy'",
                        (self.now[0] + 10 ** 6,))

    def api(self, loop, path, *args, **kwargs):
        shape = '/'.join('N' if part.isdigit() else part for part in path.split('?')[0].split('/'))
        self.calls[shape] += 1
        number = int(path.split('/pulls/')[1].split('/')[0]) if '/pulls/' in path else 1
        return {'number': number, 'state': 'open', 'draft': False,
                'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': 'o/r'}},
                'head': {'sha': HEAD, 'ref': f'b{number}', 'repo': {'full_name': 'o/r'}}}

    def ci_reason(self, row, loop, queued, expired=False):
        if row['seat'] != 'reviewer':
            return None
        self.calls['/repos/o/r/commits/SHA/check-runs'] += 1
        self.calls['/repos/o/r/commits/SHA/status'] += 1
        return HELD if self.ci_running else None

    def worker(self, held=False):
        """One worker: a claim, then the linger. A poll's spawn runs the next one at once.
        ``held``: the first is the worker that just held a row, as each of the 30 was."""
        self.workers += 1
        if self.workers > self.MAX_WORKERS:
            raise _Done
        sup = self.sup(production_claim=True)
        sup._spawn = self.worker
        sup._run_one()
        sup.held_for_ci = sup.held_for_ci or held
        sup.linger_for_ci(sleep=self.sleep)

    def sleep(self, seconds):
        self.now[0] += seconds
        if self.now[0] > self.start + self.SPAN:
            raise _Done

    def run_span(self):
        self.start = self.now[0]
        with patch('diaktoros.config.by_repo', return_value=self.loop), \
             patch('diaktoros.gh.api', side_effect=self.api), \
             patch('diaktoros.gh.reviews', side_effect=lambda *a, **k: self.calls.update(
                 ['/repos/o/r/pulls/N/reviews']) or []), \
             patch.object(run_supervisor.Supervisor, '_ci_hold_reason', self.ci_reason,
                          create=True), \
             patch.object(run_supervisor.Supervisor, '_ci_poll_defers', return_value=False,
                          create=True):
            try:
                self.worker(held=True)
            except _Done:
                pass

    def states(self):
        with ledger.connect(self.db) as con:
            return collections.Counter(row[0] for row in con.execute(
                "SELECT state FROM runs WHERE delivery LIKE 'pr%'"))

    def test_thirty_held_prs_cost_one_poller_and_no_read_while_the_seat_is_busy(self):
        self.run_span()
        reads = sum(self.calls.values())
        print(f"\n#2609 workload, {int(self.now[0] - self.start)} s: {self.workers} worker(s), "
              f"{reads} GitHub reads: {dict(self.calls)}")
        self.assertLessEqual(self.workers, 30 + 1)   # the first, and one per row first due
        self.assertEqual(reads, 0)                              # the seat never freed
        states = self.states()
        self.assertEqual(set(states) - {'pending', 'waiting'}, set())   # none started or failed
        self.assertEqual(sum(states.values()), 30)

        # CI goes green and the busy turn ends: every one of the 30 is reviewed, in turn.
        self.ci_running = False
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded' WHERE delivery='busy'")
            con.execute("UPDATE runs SET retry_at=0 WHERE state='waiting'")
        with patch('diaktoros.config.by_repo', return_value=self.loop), \
             patch('diaktoros.gh.api', side_effect=self.api), \
             patch('diaktoros.gh.reviews', return_value=[]), \
             patch.object(run_supervisor.Supervisor, '_ci_hold_reason', self.ci_reason,
                          create=True):
            self.s.recover()
            for _ in range(40):
                self.s._run_one()
        self.assertEqual(self.states(), {'succeeded': 30})


if __name__ == '__main__':
    unittest.main()
