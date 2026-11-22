"""#2609: about 30 PRs held for CI made workers start each other (9,200 in an hour) and used up
the read App's GitHub limit. One worker per ledger now polls the held rows, and a row that cannot
start now costs no GitHub read.

Real SQLite ledger; the worker runs in-process against a clock that moves only when it sleeps,
and GitHub is a patched ``gh.api`` that counts its calls. No network, no Hermes.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import time
import unittest
from unittest.mock import Mock, patch

import test_retryable_turns as retryable
from diaktoros import run_supervisor
from diaktoros.state import ledger
from diaktoros.run_supervisor import workers

HEAD = retryable.HEAD
LOOP = {'repo': 'o/r', 'base': 'main', 'read_token': 'read'}


class Volume(retryable.Base):
    def setUp(self):
        super().setUp()
        self.s = self.sup(production_claim=True)
        self.now = [time.time()]
        clock = patch('time.time', lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)

    def hold(self, delivery, pr, due_in=run_supervisor.CI_POLL_S):
        """A reviewer row held for CI, due ``due_in`` from now."""
        self.s.submit(delivery, 'o/r', pr, HEAD, 'reviewer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=? WHERE delivery=?",
                        (self.now[0] + due_in, run_supervisor.CI_HOLD + ' on ' + HEAD, delivery))

    def sleeper(self, sleeps):
        def sleep(seconds):
            sleeps.append(seconds)
            self.now[0] += seconds
        return sleep


class OnePoller(Volume):
    def test_a_second_lingerer_leaves_the_polling_to_the_first(self):
        self.hold('one', 1)
        self.s.held_for_ci = True
        first = workers._poller_lock(self.db)          # another worker is polling
        self.addCleanup(first.close)
        self.s.recover = Mock()
        self.s.linger_for_ci(sleep=lambda seconds: self.fail('a second poller waited'))
        self.s.recover.assert_not_called()
        first.close()                                   # that poller is gone: this one polls
        sleeps = []
        self.s.linger_for_ci(sleep=self.sleeper(sleeps))
        self.assertTrue(sleeps)
        self.s.recover.assert_called()
        again = workers._poller_lock(self.db)           # and it let the lock go on its way out
        self.assertNotIn(again, (None, False))
        again.close()

    def test_the_poller_stays_while_rows_keep_falling_due_up_to_its_limit(self):
        # Workers that hold a row leave it to the one poller, so the poller must stay past its
        # first window while CI keeps the rows held; it hands over after POLLER_MAX_S.
        self.hold('one', 1)
        self.hold('two', 2, due_in=40)
        start = self.now[0]

        def still_running():                            # each poll finds CI still running
            with ledger.connect(self.db) as con:
                con.execute("UPDATE runs SET retry_at=? WHERE state='waiting' AND retry_at<=?",
                            (self.now[0] + run_supervisor.CI_POLL_S, self.now[0]))
        self.s.recover = Mock(side_effect=still_running)
        self.s.held_for_ci = True
        with patch.object(run_supervisor, 'POLLER_MAX_S', 1200.0):
            self.s.linger_for_ci(sleep=self.sleeper([]))
        window = max(run_supervisor.CI_POLL_S, run_supervisor.RETRY_BASE) + 30
        self.assertGreater(self.now[0] - start, window)            # past the old fixed window
        self.assertLessEqual(self.now[0] - start, 1200.0 + window)  # and bounded
        self.assertGreaterEqual(self.s.recover.call_count, 1200 // run_supervisor.CI_POLL_S)


class NoReadForARowThatCannotStart(Volume):
    def claim_with(self, api):
        with patch('diaktoros.config.by_repo', return_value=LOOP), \
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


class AWorkerNamesItself(unittest.TestCase):
    def test_a_worker_counts_its_calls_as_the_worker(self):
        # Its entry script is run_supervisor/__main__.py, which names nothing (#2609).
        from types import SimpleNamespace
        from diaktoros import gh_calls
        args = SimpleNamespace(db='/nonexistent/ledger.sqlite', operation='_fixture-worker')
        # The worker env flag it sets is undone with the patched environment.
        with patch.object(gh_calls, '_caller', ''), \
                patch.dict('os.environ', {'DIAKTOROS_TEST_FIXTURE': '0'}), \
                patch.object(run_supervisor.waits, 'cap_stderr'):
            with self.assertRaisesRegex(SystemExit, 'fixture worker disabled'):
                run_supervisor._worker(args)
            self.assertEqual(gh_calls.caller(), 'worker')


if __name__ == '__main__':
    unittest.main()
