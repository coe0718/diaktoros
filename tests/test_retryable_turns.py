"""Issue #53/#73: a turn that failed before any external write is retried, never lost.

Real SQLite ledger and real fixture child processes (the worker runs in-process so the
test controls time); the claim's GitHub reads go through a patched ``gh.api``. Nothing
here launches Hermes, a sandbox or a network call.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from diaktoros import gate, ledger, run_supervisor
from diaktoros.run_supervisor import views, workers
from diaktoros.run_supervisor import MAX_REARMS, MAX_RETRIES, Supervisor

HEAD = 'a' * 40
CHILD = r'''
import os, sqlite3, sys, time
db, codes, mode = sys.argv[1], sys.argv[2].split(','), sys.argv[3]
counter = db + '.launches'
if os.path.exists(counter):
    with open(counter) as f:
        n = len(f.read().splitlines())
else:
    n = 0
with open(counter, 'a') as f:
    f.write('x\n')
code = int(codes[min(n, len(codes) - 1)])
con = sqlite3.connect(db, timeout=10)
# The worker marks the run 'running' just *after* spawning this child; a loaded machine can
# run the child first. Wait for the mark (bounded) instead of racing it.
row = None
for _ in range(1000):
    row = con.execute("SELECT id FROM runs WHERE state='running'").fetchone()
    if row:
        break
    time.sleep(0.01)
run = row[0]
if mode in ('claimed', 'confirmed'):
    # What the broker commits before (claimed) or after (confirmed) a review POST.
    con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,created) "
                "VALUES(?,?,?,?,?)", (run, mode, 'g', 1, 0))
    con.commit()
con.close()
if mode == 'token-at-cut':
    # A token straddling the cut at the stored size of each stream (#2083).
    token = 'ghp_' + 'A1b2C3d4' * 4
    sys.stdout.write('x' * 8000 + token + ' ' + 'y' * 8169)
    sys.stderr.write('e' * 2000 + token + ' ' + 'f' * 2025)
if code:
    print('hermes: provider error', flush=True)
    print('upstream HTTP 429: rate limited (attempt %d)' % (n + 1), file=sys.stderr)
sys.exit(code)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'ledger.sqlite'
        (self.root / 'child.py').write_text(CHILD)
        env = patch.dict(os.environ, {'HERMES_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def sup(self, codes='0', mode='none', production_claim=False):
        sup = Supervisor(self.db, fixture_mode=True, fixture_command=[
            sys.executable, str(self.root / 'child.py'), str(self.db), codes, mode])
        sup._spawn = lambda: None          # the test drives the worker in-process
        if production_claim:
            # The production claim's GitHub reads, with the fixture child as the turn.
            sup.production_config = self.root / 'unused'
            sup._run_production = sup._run_fixture
        return sup

    def launches(self):
        path = Path(str(self.db) + '.launches')
        return len(path.read_text().splitlines()) if path.exists() else 0

    def elapse(self):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET retry_at=0 WHERE state='waiting'")

    def notices(self, sup):
        out = []
        sup.notify(out.append)
        return out


class PreWriteTurnFailures(Base):
    def test_nonzero_exit_waits_with_backoff_then_succeeds(self):
        sup = self.sup(codes='3,0')
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'enqueued')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries'], row['outcome']), ('waiting', 1, 3))
        self.assertEqual(row['error'], 'turn exited with status 3')
        self.assertIn('upstream HTTP 429', row['detail'])
        self.assertAlmostEqual(row['retry_at'] - time.time(), run_supervisor.backoff(1), delta=5)
        sup.recover()                      # backoff not elapsed: nothing scheduled
        self.assertEqual(sup.get('d')['state'], 'waiting')
        self.assertEqual(self.notices(sup), [], 'a waiting run is not an operator alarm')
        self.elapse()
        sup.recover()
        self.assertEqual(sup.get('d')['state'], 'pending')
        sup._run_one()
        self.assertEqual(sup.get('d')['state'], 'succeeded')
        self.assertEqual(self.launches(), 2)

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual([run_supervisor.backoff(n) for n in (1, 2, 3)], [120, 240, 480])
        self.assertEqual(run_supervisor.backoff(20), run_supervisor.RETRY_CAP)

    def test_retry_limit_fails_with_real_reason_then_redelivery_and_retry_rearm(self):
        sup = self.sup(codes='3')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        for _ in range(MAX_RETRIES):
            self.elapse()
            sup.recover()
            sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries']), ('failed', MAX_RETRIES))
        self.assertEqual(row['error'], f'retry limit ({MAX_RETRIES} attempts): turn exited with status 3')
        [notice] = self.notices(sup)
        self.assertIn('turn exited with status 3', notice)
        self.assertIn(f'upstream HTTP 429: rate limited (attempt {MAX_RETRIES})', notice)
        self.assertIn('No external write was made', notice)
        self.assertIn('hermes dk retry --loop LOOP --pr 1 --seat reviewer', notice)
        self.assertNotIn('Do not replay', notice)
        # A new event for this head re-arms it, one attempt per event (#53/#73).
        self.assertEqual(sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')
        self.assertEqual(sup.get('d')['state'], 'pending')
        sup._run_one()
        self.assertEqual(sup.get('d')['state'], 'failed')
        self.assertEqual(len(self.notices(sup)), 1, 'the second failure is reported afresh')
        # The operator's retry resets the budget: the backoff chain starts over.
        self.assertEqual(sup.retry(row['id']), 'pending')
        sup._run_one()
        self.assertEqual((sup.get('d')['state'], sup.get('d')['retries']), ('waiting', 1))

    def test_redelivery_rearm_is_bounded(self):
        sup = self.sup(codes='3')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=?", (MAX_REARMS,))
        outcome = sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        self.assertTrue(outcome.startswith('duplicate failed:'), outcome)
        self.assertIn('hermes dk retry', outcome)
        self.assertEqual(sup.get('d')['state'], 'failed')

    def test_budget_timeout_fails_rearmable_not_auto_retried(self):
        # #49 x #53: the same budget would most likely run out again, so a budget kill is not
        # backed off and relaunched; it never wrote, so `retry` re-arms it (on a new budget).
        (self.root / 'slow.py').write_text('import time\ntime.sleep(5)\n')
        sup = Supervisor(self.db, fixture_mode=True, child_timeout=0.2,
                         fixture_command=[sys.executable, str(self.root / 'slow.py')])
        sup._spawn = lambda: None
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['error'], row['retries'], row['retry_at']),
                         ('failed', 'child timeout', 0, None))
        self.assertEqual(sup.retry(row['id'], budget=0.5), 'pending')
        self.assertEqual((sup.get('d')['state'], sup.get('d')['budget']), ('pending', 0.5))

    def test_timeout_kill_race_keeps_child_timeout_error(self):
        # #739: the child exiting between wait() timing out and killpg must not turn the
        # accurate "child timeout" into "launch/wait failed: ProcessLookupError".
        (self.root / 'slow.py').write_text('import time\ntime.sleep(5)\n')
        sup = Supervisor(self.db, fixture_mode=True, child_timeout=0.2,
                         fixture_command=[sys.executable, str(self.root / 'slow.py')])
        sup._spawn = lambda: None
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        real = os.killpg

        def racy(pid, sig):
            real(pid, sig)
            raise ProcessLookupError(3, 'No such process')
        with patch.object(run_supervisor.os, 'killpg', racy):
            sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['error']), ('failed', 'child timeout'))

    def test_duplicate_of_active_or_finished_run_is_reported_not_enqueued(self):
        sup = self.sup()
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'enqueued')
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'pending')
        sup._run_one()
        self.assertEqual(sup.submit('again', 'o/r', 1, HEAD, 'reviewer'), 'duplicate succeeded')
        self.assertEqual(self.launches(), 1)

    def test_a_token_split_at_the_fixture_tail_cut_is_redacted_whole(self):
        # #2083: the fixture worker read exactly the stored size of each stream, so a token
        # split by that cut reached output_detail without its prefix and was kept in part.
        sup = self.sup(mode='token-at-cut')
        self.assertEqual(sup.submit('d', 'o/r', 1, HEAD, 'reviewer'), 'enqueued')
        sup._run_one()
        detail = sup.get('d')['detail']
        self.assertNotIn('C3d4A1b2', detail)
        self.assertEqual(detail.count('[REDACTED]'), 2)
        self.assertTrue(detail.endswith('[REDACTED] ' + 'y' * 8169))


class PostWriteStaysQuarantined(Base):
    def test_claimed_receipt_then_crash_is_uncertain_and_never_rearmed(self):
        sup = self.sup(codes='1', mode='claimed')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual(row['state'], 'uncertain')
        outcome = sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer')
        self.assertEqual(outcome, 'duplicate uncertain')
        with self.assertRaisesRegex(ValueError, 'never replayed.*reconcile'):
            sup.retry(row['id'])
        [notice] = self.notices(sup)
        self.assertIn('Do not replay', notice)
        self.assertIn('Possible external write (run is uncertain', notice)
        # Even after the operator reconciles it, the run is not re-armed.
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET pid=NULL WHERE id=?", (row['id'],))
        sup.reconcile_uncertain(row['id'], reason='inspected', acknowledge_no_live_worker=True)
        self.assertTrue(sup.submit('again', 'o/r', 1, HEAD, 'reviewer').startswith(
            'duplicate failed: run was quarantined'))
        with self.assertRaisesRegex(ValueError, 'refused'):
            sup.retry(row['id'])
        self.assertEqual(self.launches(), 1)

    def test_confirmed_write_then_nonzero_exit_fails_without_retry(self):
        sup = self.sup(codes='1', mode='confirmed')
        sup.submit('d', 'o/r', 1, HEAD, 'reviewer')
        sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['retries']), ('failed', 0))
        self.assertEqual(sup.submit('redelivery', 'o/r', 1, HEAD, 'reviewer'),
                         'duplicate failed: review receipt confirmed — reconcile, never replayed')
        with self.assertRaisesRegex(ValueError, 'review receipt confirmed'):
            sup.retry(row['id'])
        self.assertEqual(self.launches(), 1)

    def test_push_intent_and_ruling_count_as_writes(self):
        sup = self.sup()
        sup.submit('d', 'o/r', 1, HEAD, 'fixer')
        row = sup.get('d')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', push_confirmed=1 WHERE id=?", (row['id'],))
        with self.assertRaisesRegex(ValueError, 'fixer push recorded'):
            sup.retry(row['id'])
        sup.submit('a', 'o/r', 2, HEAD, 'adjudicator', turn_key='breach:3')
        adj = sup.get('a')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed' WHERE id=?", (adj['id'],))
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (adj['id'], 'o/r', 2, HEAD, 'breach:3',
                                                     'ACCEPT', 'ok', 0, 0))
        with self.assertRaisesRegex(ValueError, 'ruling recorded'):
            sup.retry(adj['id'])


class ClaimTimeReads(Base):
    LOOP = {'repo': 'o/r', 'base': 'main', 'read_token': 'read'}

    def pull(self, **changes):
        pr = {'number': 1, 'state': 'open', 'draft': False,
              'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': 'o/r'}},
              'head': {'sha': HEAD, 'repo': {'full_name': 'o/r'}}}
        pr.update(changes)
        return pr

    def setUp(self):
        super().setUp()
        self.LOOP = {**self.LOOP, 'state_dir': str(self.root / 'state')}
        self.s = self.sup(production_claim=True)
        self.s.submit('review', 'o/r', 1, HEAD, 'reviewer')

    def claim(self, pr):
        with patch('diaktoros.config.by_repo', return_value=self.LOOP), \
             patch('diaktoros.gh.api', return_value=pr), \
             patch('diaktoros.gh.reviews', return_value=[]):
            return self.s._claim()

    def test_502_at_claim_is_a_read_retry_then_the_run_succeeds(self):
        self.assertIsNone(self.claim(None))           # gh.api answers None for an HTTP 502
        row = self.s.get('review')
        # A counted, visible, backed-off retry — never an invisible pending row (Arbiter on #97).
        self.assertEqual((row['state'], row['attempts'], row['retries']), ('waiting', 0, 1))
        self.assertIn('claim-time read failed', row['error'])
        self.elapse()
        self.s.recover()
        with patch('diaktoros.config.by_repo', return_value=self.LOOP), \
             patch('diaktoros.gh.api', return_value=self.pull()), \
             patch('diaktoros.gh.reviews', return_value=[]):
            self.s._run_one()
        self.assertEqual(self.s.get('review')['state'], 'succeeded')

    def clock(self):
        """A clock that moves only when a worker sleeps, so the wait is real elapsed time (F2 of
        #1808): a sleep that ends past the linger's deadline leaves a retry nobody scheduled."""
        now = [time.time()]
        clock = patch('time.time', lambda: now[0])
        clock.start()
        self.addCleanup(clock.stop)
        return now

    def worker(self, sleeps, now, read=lambda: None):
        """What the production worker does (``main``): one claim, then the linger. Each wait in
        ``sleeps`` advances the clock ``now``; ``read`` is what GitHub answers for the PR."""
        def sleep(seconds):
            sleeps.append(seconds)
            self.marked.append(workers.lingering_pids(self.s.db))
            now[0] += seconds
        spawned = []
        self.marked = []                # who carries the linger mark at each wait (#1811)
        self.s._spawn = lambda: spawned.append(1)
        with patch('diaktoros.config.by_repo', return_value=self.LOOP), \
             patch('diaktoros.gh.api', side_effect=lambda *a, **k: read()), \
             patch('diaktoros.gh.reviews', return_value=[]):
            self.s._run_one()
            self.s.linger_for_ci(sleep=sleep)
        return spawned

    def test_a_worker_that_backed_off_a_failed_read_schedules_the_retry_itself(self):
        # #1302: a kick whose claim-time read failed sat 'waiting' until the next watchdog
        # sweep. The worker that backed it off now stays for the first backoff and re-arms it.
        sleeps = []
        spawned = self.worker(sleeps, self.clock())
        self.assertEqual(len(sleeps), 1, sleeps)
        self.assertAlmostEqual(sleeps[0], run_supervisor.backoff(1) + 1, delta=1)
        row = self.s.get('review')
        self.assertEqual((row['state'], row['retries']), ('pending', 1))
        self.assertEqual(len(spawned), 1)          # recover() armed a fresh worker

    def test_the_worker_carries_the_linger_mark_while_it_waits_and_drops_it_after(self):
        # #1811: the mark is what the test cleanup ends a lingering worker by.
        sleeps = []
        self.worker(sleeps, self.clock())
        self.assertEqual(self.marked, [{os.getpid()}] * len(sleeps))
        self.assertTrue(sleeps)
        self.assertEqual(workers.lingering_pids(self.s.db), set())

    def test_the_worker_is_unmarked_while_it_reads_and_recovers(self):
        # F1 of #1823: only the sleep is a safe wait. A worker hung in recover() (or in its
        # database reads) is at work; a mark there would let a test end it and pass its exit check.
        seen = []
        real = self.s.recover

        def recover():
            seen.append(workers.lingering_pids(self.s.db))
            return real()
        self.s.recover = recover
        sleeps = []
        self.worker(sleeps, self.clock())
        self.assertTrue(sleeps)
        self.assertEqual(seen, [set()])

    def test_the_linger_window_outlasts_the_first_backoff(self):
        # F2 of #1808: with the old window (CI_POLL_S + 30 = 120 s) the sleep to a retry due at
        # 120 s ends at 121 s, past the deadline, and the loop left without calling recover().
        # The clock here moves only by what the worker sleeps, so that case is real.
        self.assertGreaterEqual(run_supervisor.backoff(1), run_supervisor.CI_POLL_S + 30)
        now = self.clock()
        self.worker([], now)
        self.assertEqual(self.s.get('review')['state'], 'pending')

    def test_a_worker_keeps_its_own_retry_when_another_row_is_due_first(self):
        # F3 of #1808: a CI hold due in 5 s is released first, but this worker's own retry (due
        # in 120 s) may have no other worker: it stays for that one instead of leaving for the sweep.
        now = self.clock()
        self.s.submit('ci', 'o/r', 2, HEAD, 'reviewer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=? WHERE delivery='ci'",
                        (now[0] + 5, f'{run_supervisor.CI_HOLD} on aaaaaaa'))
        sleeps = []
        self.worker(sleeps, now)
        self.assertEqual(len(sleeps), 2, sleeps)
        self.assertEqual({self.s.get(d)['state'] for d in ('ci', 'review')}, {'pending'})

    def test_a_worker_does_not_wait_for_a_backoff_past_its_window(self):
        # The third failed read backs off 8 minutes: the watchdog sweep's, not a held process's.
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET retries=2")
        sleeps = []
        self.worker(sleeps, self.clock())
        self.assertEqual((sleeps, self.s.get('review')['state']), ([], 'waiting'))

    def test_a_kicked_review_is_started_by_its_own_worker_not_the_watchdog_sweep(self):
        # #1302 at the kick boundary: ``hermes dk review`` queues its reviewer through
        # ``gate.enqueue_isolated``. The first claim-time read fails; the worker that backed the
        # row off re-arms it itself (no sweep's ``recover`` runs here), and the next worker runs
        # the reviewer to its end. This fails without the fix: the row stays 'waiting'.
        with ledger.connect(self.db) as con:
            con.execute('DELETE FROM runs')              # the row setUp queued: the kick makes its own
        with patch.object(gate, 'isolated_supervisor', return_value=self.s):
            self.assertEqual(gate.enqueue_isolated(self.LOOP, 'reviewer', 1, HEAD), 'enqueued')
        delivery = f'o/r:1:{HEAD}:reviewer'
        sleeps = []
        spawned = self.worker(sleeps, self.clock())
        self.assertEqual((len(sleeps), len(spawned)), (1, 1), sleeps)
        with patch('diaktoros.config.by_repo', return_value=self.LOOP), \
             patch('diaktoros.gh.api', return_value=ClaimTimeReads.pull(self)), \
             patch('diaktoros.gh.reviews', return_value=[]):
            self.s._run_one()
        self.assertEqual(self.s.get(delivery)['state'], 'succeeded')

    def test_a_read_github_keeps_refusing_is_bounded_visible_and_noticed(self):
        for attempt in range(1, MAX_RETRIES + 1):
            self.assertIsNone(self.claim(None))
            row = self.s.get('review')
            if attempt < MAX_RETRIES:
                self.assertEqual((row['state'], row['retries']), ('waiting', attempt))
                self.assertAlmostEqual(row['retry_at'] - time.time(),
                                       run_supervisor.backoff(attempt), delta=5)
                # status/explain show it, with the reason and when it is tried again.
                [view] = views.read_only_view(self.db, 'o/r', 1)
                line = views.describe_run(view, 'widgets')
                self.assertIn('claim-time read failed: PR unreadable', line)
                self.assertIn(f'attempt {attempt + 1} of {MAX_RETRIES} due in', line)
                self.assertEqual(self.notices(self.s), [])
                self.elapse()
                self.s.recover()
        row = self.s.get('review')
        self.assertEqual(row['state'], 'failed')
        self.assertIn(f'retry limit ({MAX_RETRIES} attempts): claim-time read failed: '
                      'PR unreadable', row['error'])
        [notice] = self.notices(self.s)
        self.assertIn('claim-time read failed', notice)
        self.assertIn('No external write was made', notice)
        [view] = views.read_only_view(self.db, 'o/r', 1)
        self.assertIsNone(view['write'])                              # pre-write: re-armable
        self.assertEqual(self.s.retry(row['id']), 'pending')          # the operator re-arms it

    def test_a_raising_claim_read_names_the_exception(self):
        with patch('diaktoros.config.by_repo', return_value=self.LOOP), \
             patch('diaktoros.gh.api', side_effect=OSError('connection reset')):
            self.assertIsNone(self.s._claim())
        self.assertIn('OSError: connection reset', self.s.get('review')['error'])

    def test_draft_waits_like_the_fixer(self):
        self.assertIsNone(self.claim(self.pull(draft=True)))
        self.assertEqual(self.s.get('review')['state'], 'pending')
        self.assertIsNotNone(self.claim(self.pull()))

    def test_closed_then_reopen_rearms(self):
        self.assertIsNone(self.claim(self.pull(state='closed')))
        row = self.s.get('review')
        self.assertEqual((row['state'], row['error']), ('cancelled', 'PR closed before the review started'))
        self.assertEqual(self.s.submit('reopened', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')
        self.assertIsNotNone(self.claim(self.pull()))

    def test_unresolvable_generation_fails_with_reason_and_is_rearmable(self):
        self.assertIsNone(self.claim(self.pull(base={'ref': 'other', 'sha': 'b' * 40,
                                                     'repo': {'full_name': 'o/r'}})))
        row = self.s.get('review')
        self.assertEqual((row['state'], row['error']),
                         ('failed', 'review generation unavailable: stale or unresolvable generation'))
        self.assertIsNone(views.read_only_view(self.db, 'o/r', 1)[0]['write'])
        self.assertEqual(self.s.submit('retargeted-back', 'o/r', 1, HEAD, 'reviewer'), 'rearmed')


class ProductionClassification(unittest.TestCase):
    def test_transient_versus_terminal(self):
        from diaktoros import seat_model, trusted_fetch
        yes = [run_supervisor.RetryableError('x'), TimeoutError(), ConnectionResetError(),
               trusted_fetch.FetchDenied('GitHub response unavailable'),
               seat_model.SeatModelError('profile p: Hermes did not resolve within 30s')]
        no = [ValueError('PR head moved'), trusted_fetch.FetchDenied('stale PR head'),
              seat_model.SeatModelError('profile p (bedrock): unsupported'),
              FileNotFoundError('live broker capability required')]
        self.assertTrue(all(run_supervisor.retryable(e) for e in yes))
        self.assertFalse(any(run_supervisor.retryable(e) for e in no))

    def test_stored_detail_keeps_8kb_of_stdout_redacted(self):
        # #1658: the stored detail keeps the last 8 KB of stdout, with tokens redacted.
        token = 'ghp_' + 'A1b2C3d4' * 4
        stdout = 'start ' + 'x' * 20000 + ' ' + 'y' * 7000 + f' {token} end'
        detail = run_supervisor.output_detail(stdout, 'err')
        self.assertIn('y' * 7000, detail)
        self.assertGreaterEqual(len(detail.encode()), 8000)
        self.assertNotIn(token, detail)
        self.assertIn('[REDACTED]', detail)

    def test_tail_is_bounded_and_printable(self):
        text = run_supervisor.tail('a\x1b[31m' + 'z' * 5000)
        self.assertLessEqual(len(text.encode()), run_supervisor.DETAIL_BYTES + 10)
        self.assertNotIn('\x1b', text)


if __name__ == '__main__':
    unittest.main()


class OperatorCommands(unittest.TestCase):
    """``hermes dk retry`` and the ledger lines ``status``/``explain`` print."""

    def setUp(self):
        import json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        loops = home / 'review-loops.d'
        loops.mkdir()
        loop = {"id": "widgets", "repo": "acme/widgets", "base": "main", "cap": 3,
                "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                "seats": {"reviewer": {"profile": "r", "route": "review"},
                          "fixer": {"profile": "f", "route": "fix"}},
                "state_dir": str(home / "state"), "tokens": {}, "read_token": "reader",
                "host": "http://127.0.0.1:9"}
        (loops / 'widgets.json').write_text(json.dumps(loop))
        env = patch.dict(os.environ, {'HERMES_HOME': str(home), 'DIAKTOROS_CONFIG_DIR': str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.db = home / 'state' / 'diaktoros-runs.sqlite'
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=['true'])
        self.sup._spawn = lambda: None
        self.sup.submit('r', 'acme/widgets', 7, HEAD, 'reviewer')
        self.sup.submit('f', 'acme/widgets', 7, HEAD, 'fixer')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=4, "
                        "error='retry limit (4 attempts): turn exited with status 3', "
                        "detail='stderr: upstream HTTP 429' WHERE delivery='r'")
            con.execute("UPDATE runs SET state='uncertain', launch_intent=1, "
                        "error='worker lost after launch intent' WHERE delivery='f'")

    def run_cli(self, func, **kw):
        import argparse
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(argparse.Namespace(**kw))
        return code, out.getvalue()

    def test_retry_leaves_a_failed_shadow_alone_unless_it_is_named(self):
        # #218: a shadow run is no part of the PR's loop; only `--seat shadow` re-arms it.
        from diaktoros import cli
        self.sup.submit('s', 'acme/widgets', 7, HEAD, 'shadow')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE delivery='s'")
        self.keep_snapshot(7)
        _, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat=None)
        self.assertNotIn('shadow', out)
        self.assertEqual(self.sup.get('s')['state'], 'failed')
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat='shadow')
        self.assertIn('shadow #7 @ aaaaaaa re-armed', out)
        self.assertEqual(self.sup.get('s')['state'], 'pending')

    def keep_snapshot(self, pr):
        from diaktoros import config, state as state_mod
        state_mod.state_for(config.by_repo('acme/widgets')).shadow_findings_put(pr, HEAD, {}, set())

    def test_retry_refuses_a_shadow_whose_findings_snapshot_is_gone(self):
        # #1013: a failed shadow's snapshot is evicted by design; re-arming it would judge it on
        # the live findings ledger. The operator's retry is a run, so it is refused, and says why.
        from diaktoros import cli, config, state as state_mod
        self.sup.submit('s', 'acme/widgets', 7, HEAD, 'shadow')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE delivery='s'")
        st = state_mod.state_for(config.by_repo('acme/widgets'))
        self.assertIsNone(st.shadow_findings_get(7, HEAD))
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat='shadow')
        self.assertEqual(code, 2, out)
        self.assertIn('findings snapshot', out)
        self.assertNotIn('re-armed', out)
        self.assertEqual(self.sup.get('s')['state'], 'failed')
        # With the snapshot still on file the same retry is allowed.
        self.keep_snapshot(7)
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat='shadow')
        self.assertIn('re-armed', out)
        self.assertEqual(self.sup.get('s')['state'], 'pending')
        self.assertEqual(st.shadow_findings_get(7, HEAD), {})

    def test_supervisor_retry_refuses_a_shadow_without_a_snapshot(self):
        self.sup.submit('s', 'acme/widgets', 7, HEAD, 'shadow')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE delivery='s'")
        with self.assertRaisesRegex(ValueError, 'findings snapshot'):
            self.sup.retry(self.sup.get('s')['id'])
        self.assertEqual(self.sup.get('s')['state'], 'failed')

    def test_retry_rearms_prewrite_and_refuses_uncertain(self):
        from diaktoros import cli
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat=None)
        self.assertEqual(code, 2, out)            # one refusal makes the command exit 2
        self.assertIn('reviewer #7 @ aaaaaaa re-armed (was failed: retry limit', out)
        self.assertIn('fixer #7 @ aaaaaaa uncertain: refused: run is uncertain', out)
        self.assertIn('run_supervisor reconcile DB', out)
        self.assertIn('no private runtime file', out)
        self.assertEqual(self.sup.get('r')['state'], 'pending')
        self.assertEqual(self.sup.get('r')['retries'], 0)
        self.assertEqual(self.sup.get('f')['state'], 'uncertain')
        code, out = self.run_cli(cli.cmd_retry, loop='widgets', pr=7, seat='reviewer')
        self.assertEqual(code, 2)
        self.assertIn('nothing to retry', out)
        with ledger.connect(self.db) as con:           # one step for the report (#678), not two
            self.assertEqual(con.execute("SELECT repo, pr, action FROM operator_actions")
                             .fetchall(), [('acme/widgets', 7, 'retry')])

    def test_retry_names_the_pr_head_not_a_supersession_bumped_timestamp(self):
        # #126: bare `retry` picked the head by `max(updated)`, but the claim path bumps `updated`
        # when it cancels a row for a head the PR has since left. So a supersession at an
        # abandoned head can outrank the recoverable row at the PR's real head, and `retry` names
        # the abandoned head, offers nothing, and prints a false "nothing to retry". The head now
        # comes from the PR itself, falling back to the newest head that still has an offerable
        # row when the read fails — never a timestamp supersession rewrites.
        import argparse
        import contextlib
        import io
        from diaktoros import cli, gh
        # Three rows: a recoverable failed run at the PR's real head (created first, so its
        # `updated` is older), and a superseded cancellation at an abandoned head (created last
        # and bumped newest by the claim path's supersession write).
        self.sup.submit('r', 'acme/widgets', 7, HEAD, 'reviewer')          # real head, recoverable
        self.sup.submit('s', 'acme/widgets', 7, 'b' * 40, 'fixer')         # abandoned head
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=4, "
                        "error='retry limit (4 attempts): turn exited with status 3' "
                        "WHERE delivery='r'")
            # Supersession at the abandoned head: newest `updated`, but not offerable as a head.
            con.execute("UPDATE runs SET state='cancelled', "
                        "error='PR head moved before the review started', updated=? "
                        "WHERE delivery='s'", (time.time() + 1000,))
        pr = {'number': 7, 'state': 'open', 'head': {'sha': HEAD}}
        with patch.object(gh, 'pr', return_value=pr):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.cmd_retry(argparse.Namespace(loop='widgets', pr=7, seat=None))
        text = out.getvalue()
        # The real head is named and its recoverable row is offered — not the abandoned head.
        self.assertIn(f'reviewer #7 @ {HEAD[:7]} re-armed', text)
        self.assertNotIn('b' * 7, text, 'the abandoned head must not be chosen')
        self.assertNotIn('nothing to retry', text)
        self.assertEqual(self.sup.get('r')['state'], 'pending')

    def test_retry_degrades_to_offerable_head_when_the_pr_read_fails(self):
        # The tiebreaker half of #126: with the supersession disagreement present but the PR read
        # unavailable (gh.pr -> None), retry must fall back to the newest head that still has an
        # offerable row — never the supersession-bumped cancelled row at the abandoned head.
        import argparse
        import contextlib
        import io
        from diaktoros import cli, gh
        self.sup.submit('r', 'acme/widgets', 7, HEAD, 'reviewer')          # real head, recoverable
        self.sup.submit('s', 'acme/widgets', 7, 'b' * 40, 'fixer')         # abandoned head
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retries=4, "
                        "error='retry limit (4 attempts): turn exited with status 3' "
                        "WHERE delivery='r'")
            con.execute("UPDATE runs SET state='cancelled', "
                        "error='PR head moved before the review started', updated=? "
                        "WHERE delivery='s'", (time.time() + 1000,))
        with patch.object(gh, 'pr', return_value=None):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.cmd_retry(argparse.Namespace(loop='widgets', pr=7, seat=None))
        text = out.getvalue()
        self.assertNotIn('b' * 7, text, 'a supersession bump must not pick the head')
        self.assertIn(f'reviewer #7 @ {HEAD[:7]}', text)

    def test_ledger_lines_show_reason_tail_and_next_step(self):
        from diaktoros import config
        from diaktoros.cli import operations as cli_operations
        loop = config.load_id('widgets')
        import contextlib
        import io
        import gc
        out = io.StringIO()
        # setUp's connections (Supervisor._connect's `with` commits but does not close) keep the
        # ledger in WAL mode with -wal/-shm files beside it until the garbage collector closes
        # them — at a moment that depends on everything that ran before this test. Close them
        # now, so the snapshot below is the ledger at rest and only the read under test can
        # change the directory (#97 CI: 3.13 collected them in the middle of the read).
        gc.collect()
        before = sorted(p.name for p in self.db.parent.iterdir())
        # The ledger, and the host's record that it exists (#108: a vanished ledger is reported).
        self.assertEqual(before, ['diaktoros-runs.sqlite', 'diaktoros-runs.sqlite.present'])
        with contextlib.redirect_stdout(out):
            cli_operations._print_ledger_runs(loop, 7, '  run: ', limit=6)
        text = out.getvalue()
        self.assertIn('reviewer #7 @ aaaaaaa failed — retry limit (4 attempts): turn exited with '
                      'status 3; no external write — re-arm: hermes dk retry --loop '
                      'widgets --pr 7 --seat reviewer', text)
        self.assertIn('| stderr: upstream HTTP 429', text)
        self.assertIn('fixer #7 @ aaaaaaa uncertain — worker lost after launch intent; may have '
                      'written (run is uncertain', text)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before,
                         'reading the ledger for explain/status writes nothing')

    def test_armed_sweep_schedules_due_retries_only_with_a_runtime(self):
        from diaktoros import config, gate
        loop = config.load_id('widgets')
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=0 WHERE delivery='r'")
        with patch.object(Supervisor, '_spawn') as spawn:
            self.assertFalse(gate.resume_isolated(loop))       # no private runtime: nothing
            spawn.assert_not_called()
            runtime = Path(os.environ['HERMES_HOME']) / 'diaktoros-runtime.json'
            runtime.write_text('{}')
            runtime.chmod(0o600)
            self.assertTrue(gate.resume_isolated(loop))
            spawn.assert_called_once_with()
        self.assertEqual(self.sup.get('r')['state'], 'pending')

    def test_breach_resume_hands_back_only_this_heads_adjudicating_marker(self):
        from diaktoros import config, state
        st = state.LoopState(config.load_id('widgets'))
        st.breach_set(7, {'pr': 7, 'head': HEAD, 'rounds': 3, 'status': 'awaiting-adjudication'})
        self.assertIsNone(st.breach_resume(7, HEAD, 3), 'not adjudicating: left alone')
        self.assertIsNotNone(st.breach_start(7, HEAD, 3))
        self.assertIsNone(st.breach_resume(7, 'b' * 40, 3))
        self.assertIsNone(st.breach_resume(7, HEAD, 2))
        self.assertEqual(st.breach_resume(7, HEAD, 3)['status'], 'awaiting-adjudication')
        self.assertIsNotNone(st.breach_start(7, HEAD, 3), 'the retry can start it again')
