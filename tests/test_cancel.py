"""Issue #1148: `hermes dk cancel` stops a queued, waiting or running isolated run.

Real SQLite ledger and real fixture child processes; the worker runs in-process (a thread for
a running turn) so the test controls time. Nothing here launches Hermes, a sandbox or a
network call.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from diaktoros import cancel_action, contained, ledger, retry_action, run_supervisor
from diaktoros.run_supervisor import OPERATOR_CANCEL, OPERATOR_STOPPED, Supervisor

HEAD = 'a' * 40
OLD = 'b' * 40
REPO = 'o/r'
LOOP = {'id': 'loop', 'repo': REPO}
# A child that writes its pid, optionally records a write-ahead receipt, then runs until killed
# (or exits with the given code after a short while).
CHILD = r'''
import os, sqlite3, sys, time
db, mode, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(db + '.launches', 'a') as f:
    f.write('%d\n' % os.getpid())
con = sqlite3.connect(db, timeout=10)
row = None
for _ in range(1000):
    row = con.execute("SELECT id FROM runs WHERE state='running'").fetchone()
    if row:
        break
    time.sleep(0.01)
if mode == 'wrote':
    # What the broker commits after a confirmed review POST.
    con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,created) "
                "VALUES(?,?,?,?,?)", (row[0], 'confirmed', 'g', 1, 0))
    con.commit()
con.close()
time.sleep(60 if mode != 'quick' else 0.3)
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

    def sup(self, mode='run', code=0):
        # A short lease makes the heartbeat (and so the cancel read) beat every second.
        sup = Supervisor(self.db, fixture_mode=True, lease_seconds=3, fixture_command=[
            sys.executable, str(self.root / 'child.py'), str(self.db), mode, str(code)])
        sup._spawn = lambda: None          # the test drives the worker in-process
        return sup

    def launched(self):
        path = Path(str(self.db) + '.launches')
        return [int(pid) for pid in path.read_text().split()] if path.exists() else []

    def wait_for(self, sup, delivery, states, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sup.get(delivery)['state'] in states:
                return sup.get(delivery)
            time.sleep(0.05)
        self.fail(f"run never reached {states}: {sup.get(delivery)['state']}")

    def running(self, sup, delivery='d', seat='reviewer'):
        """Submit a run and start its worker in a thread; return once its turn is running."""
        sup.submit(delivery, REPO, 1, HEAD, seat)
        worker = threading.Thread(target=sup._run_one, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 30)
        self.wait_for(sup, delivery, ('running',))
        deadline = time.monotonic() + 10
        while not self.launched() and time.monotonic() < deadline:
            time.sleep(0.05)
        return worker

    def set_state(self, delivery, state, **extra):
        with ledger.connect(self.db) as con:
            sets = ', '.join(['state=?'] + [f'{k}=?' for k in extra])
            con.execute(f"UPDATE runs SET {sets} WHERE delivery=?",
                        (state, *extra.values(), delivery))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not yet reaped by a parent other than us) is not a live turn.
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split(')')[-1].split()[0] != 'Z'
    except OSError:
        return False


class NotStarted(Base):
    def test_a_waiting_run_is_cancelled_and_no_redelivery_or_sweep_rearms_it(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        self.set_state('d', 'waiting', retry_at=0, error='turn exited with status 3')
        run_id = sup.get('d')['id']
        self.assertEqual(sup.cancel(run_id), {'was': 'waiting', 'state': 'cancelled'})
        row = sup.get('d')
        self.assertEqual((row['state'], row['error'], row['retry_at']),
                         ('cancelled', OPERATOR_CANCEL, None))
        # A redelivered event for the same head, and the sweep, leave it cancelled.
        outcome = sup.submit('redelivery', REPO, 1, HEAD, 'reviewer')
        self.assertTrue(outcome.startswith('duplicate cancelled: cancelled by an operator'), outcome)
        sup.recover()
        self.assertEqual(sup.get('d')['state'], 'cancelled')
        sup._run_one()
        self.assertEqual(self.launched(), [], 'a cancelled run never launches')
        # A new head still queues normally.
        self.assertEqual(sup.submit('new-head', REPO, 1, OLD, 'reviewer'), 'enqueued')
        # And `retry` re-arms it on purpose.
        self.assertEqual(sup.retry(run_id), 'pending')

    def test_a_waiting_run_with_a_write_ahead_record_is_quarantined_not_cancelled(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        run_id = sup.get('d')['id']
        self.set_state('d', 'waiting')
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,created) "
                        "VALUES(?,?,?,?,?)", (run_id, 'confirmed', 'g', 1, 0))
        self.assertEqual(sup.cancel(run_id), {'was': 'waiting', 'state': 'uncertain'})
        self.assertIn('after it may have written (review receipt confirmed)', sup.get('d')['error'])

    def test_a_pending_run_is_cancelled(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        self.assertEqual(sup.cancel(sup.get('d')['id'])['state'], 'cancelled')
        sup._run_one()
        self.assertEqual(self.launched(), [])

    def test_a_claimed_run_is_cancelled_and_its_worker_never_launches_it(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        claim = sup._claim()
        self.assertIsNotNone(claim)
        self.assertEqual(sup.cancel(claim[0]), {'was': 'claimed', 'state': 'cancelled'})
        # The worker carries on from its claim: the launch finds the claim gone.
        with patch.object(sup, '_claim', return_value=claim):
            sup._run_one()
        self.assertEqual(self.launched(), [])
        self.assertEqual(sup.get('d')['state'], 'cancelled')

    def test_a_finished_run_is_refused_with_its_state(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        for state in ('succeeded', 'failed', 'cancelled', 'uncertain'):
            self.set_state('d', state)
            with self.assertRaises(ValueError) as caught:
                sup.cancel(sup.get('d')['id'])
            self.assertIn(f'run is {state}', str(caught.exception))
            self.assertEqual(sup.get('d')['state'], state)

    def test_status_lists_it_as_an_operator_cancel_with_the_retry(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        sup.cancel(sup.get('d')['id'])
        [row] = sup.status(REPO, 1)
        self.assertEqual((row['state'], row['error']), ('cancelled', OPERATOR_CANCEL))
        step = run_supervisor.next_step(row, 'loop')
        self.assertIn('cancelled by an operator', step)
        self.assertIn('hermes dk retry --loop loop --pr 1 --seat reviewer', step)
        self.assertNotIn('failed', run_supervisor.describe_run(row, 'loop').split(' — ')[0])

    def test_retry_rearms_an_operator_cancel_and_clears_the_stop_request(self):
        sup = self.sup()
        sup.submit('d', REPO, 1, HEAD, 'reviewer')
        run_id = sup.get('d')['id']
        sup.cancel(run_id)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        with sup._connect() as con:
            self.assertEqual([r['id'] for r in retry_action.plan(con, REPO, 1)['rearmable']],
                             [run_id])
        self.assertEqual(sup.retry(run_id), 'pending')
        self.assertIsNone(sup.get('d')['cancel_requested'])


class Running(Base):
    def test_a_running_turn_is_stopped_and_the_run_cancelled(self):
        sup = self.sup()
        worker = self.running(sup)
        [pid] = self.launched()
        self.assertTrue(alive(pid))
        self.assertEqual(sup.cancel(sup.get('d')['id']), {'was': 'running', 'state': 'stopping'})
        row = self.wait_for(sup, 'd', ('cancelled', 'uncertain', 'failed', 'waiting', 'succeeded'))
        worker.join(10)
        self.assertEqual((row['state'], row['error']), ('cancelled', OPERATOR_STOPPED))
        self.assertFalse(alive(pid), 'the turn\'s process was stopped')
        self.assertIsNone(row['retry_at'])
        # A redelivery never re-arms the stopped run.
        self.assertIn('cancelled by an operator', sup.submit('again', REPO, 1, HEAD, 'reviewer'))

    def test_a_running_turn_with_a_write_ahead_record_becomes_uncertain(self):
        sup = self.sup(mode='wrote')
        worker = self.running(sup)
        sup.cancel(sup.get('d')['id'])
        row = self.wait_for(sup, 'd', ('cancelled', 'uncertain', 'failed', 'waiting', 'succeeded'))
        worker.join(10)
        self.assertEqual(row['state'], 'uncertain')
        self.assertIn('after it may have written (review receipt confirmed)', row['error'])
        self.assertIn('never replayed', row['error'])

    def test_a_turn_that_fails_on_its_own_after_the_request_is_cancelled_not_retried(self):
        # The turn ends (non-zero) before the heartbeat reads the request: still the operator's.
        sup = self.sup(mode='quick', code=3)
        with patch.object(sup, '_heartbeat', lambda *a: None):
            sup.submit('d', REPO, 1, HEAD, 'reviewer')
            worker = threading.Thread(target=sup._run_one, daemon=True)
            worker.start()
            self.wait_for(sup, 'd', ('running',))
            sup.cancel(sup.get('d')['id'])
            worker.join(20)
        row = sup.get('d')
        self.assertEqual((row['state'], row['error'], row['retry_at']),
                         ('cancelled', OPERATOR_STOPPED, None))

    def test_a_turn_that_succeeds_before_the_stop_stays_succeeded(self):
        sup = self.sup(mode='quick', code=0)
        with patch.object(sup, '_heartbeat', lambda *a: None):
            sup.submit('d', REPO, 1, HEAD, 'reviewer')
            worker = threading.Thread(target=sup._run_one, daemon=True)
            worker.start()
            self.wait_for(sup, 'd', ('running',))
            sup.cancel(sup.get('d')['id'])
            worker.join(20)
        self.assertEqual(sup.get('d')['state'], 'succeeded')


class Selection(Base):
    def test_pr_and_seat_pick_only_that_seats_run(self):
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        sup.submit('f', REPO, 1, HEAD, 'fixer')
        result = cancel_action.perform(LOOP, pr=1, seat='fixer', ledger=self.db)
        self.assertEqual([(e['seat'], e['state']) for e in result['cancelled']],
                         [('fixer', 'cancelled')])
        self.assertEqual(sup.get('r')['state'], 'pending')
        self.assertEqual(sup.get('f')['state'], 'cancelled')

    def test_pr_alone_cancels_every_open_run_at_the_current_head_and_lists_them(self):
        sup = self.sup()
        sup.submit('old', REPO, 1, OLD, 'reviewer')
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        sup.submit('s', REPO, 1, HEAD, 'shadow')
        sup.submit('done', REPO, 1, HEAD, 'fixer')
        self.set_state('done', 'succeeded')
        result = cancel_action.perform(LOOP, pr=1, ledger=self.db, resolve_head=lambda: HEAD)
        self.assertEqual(sorted(e['seat'] for e in result['cancelled']), ['reviewer', 'shadow'])
        self.assertEqual([(e['seat'], e['head7']) for e in result['left']],
                         [('reviewer', OLD[:7])])
        self.assertEqual(sup.get('old')['state'], 'pending')
        self.assertEqual(sup.get('done')['state'], 'succeeded')
        printed = '\n'.join(cancel_action.lines(result))
        self.assertIn(f'reviewer #1 @ {HEAD[:7]} cancelled (was pending)', printed)
        self.assertIn('hermes dk retry --loop loop --pr 1 --seat shadow', printed)
        self.assertIn('left alone', printed)

    def test_a_run_id_from_another_repo_or_a_finished_run_is_refused(self):
        sup = self.sup()
        sup.submit('d', 'x/y', 1, HEAD, 'reviewer')
        result = cancel_action.perform(LOOP, run_id=sup.get('d')['id'], ledger=self.db)
        self.assertIn('no run', result['message'])
        self.assertEqual(sup.get('d')['state'], 'pending')
        sup.submit('e', REPO, 2, HEAD, 'reviewer')
        self.set_state('e', 'succeeded')
        result = cancel_action.perform(LOOP, run_id=sup.get('e')['id'], ledger=self.db)
        self.assertFalse(result['ok'])
        self.assertIn('run is succeeded', result['refused'][0]['reason'])

    def test_a_confirmation_that_no_longer_matches_cancels_nothing(self):
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        with sup._connect() as con:
            shown = cancel_action.snapshot(cancel_action.plan(con, REPO, 1))
        sup.submit('f', REPO, 1, HEAD, 'fixer')          # a new run since the preview
        result = cancel_action.perform(LOOP, pr=1, ledger=self.db, expected=shown)
        self.assertIn('changed since you confirmed', result['message'])
        self.assertEqual((sup.get('r')['state'], sup.get('f')['state']), ('pending', 'pending'))

    def test_a_cancel_is_recorded_as_an_operator_step(self):
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        cancel_action.perform(LOOP, pr=1, ledger=self.db)
        with ledger.connect(self.db) as con:
            actions = [r[0] for r in con.execute("SELECT action FROM operator_actions")]
        self.assertEqual(actions, ['cancel'])


class Cli(Base):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                patch('diaktoros.config.load_id', return_value=LOOP), \
                patch('diaktoros.cli.operations._ledger_path', return_value=self.db), \
                patch('diaktoros.gh.pr', return_value={'head': {'sha': HEAD}}):
            from diaktoros.cli import operations
            args = SimpleNamespace(loop='loop', run=None, pr=None, seat=None)
            for key, value in zip(argv[::2], argv[1::2]):
                setattr(args, key, value)
            code = operations.cmd_cancel(args)
        return code, out.getvalue()

    def test_the_command_cancels_and_says_how_to_rearm(self):
        sup = self.sup()
        sup.submit('r', REPO, 7, HEAD, 'reviewer')
        code, out = self.run_cli('pr', 7)
        self.assertEqual(code, 0)
        self.assertIn(f'[loop] reviewer #7 @ {HEAD[:7]} cancelled (was pending)', out)
        self.assertIn('hermes dk retry --loop loop --pr 7 --seat reviewer', out)

    def test_the_command_refuses_a_finished_run_by_state(self):
        sup = self.sup()
        sup.submit('r', REPO, 7, HEAD, 'reviewer')
        self.set_state('r', 'succeeded')
        code, out = self.run_cli('run', sup.get('r')['id'])
        self.assertEqual(code, 2)
        self.assertIn('not cancelled: run is succeeded', out)

    def test_the_parser_has_cancel(self):
        import argparse
        from diaktoros.cli import operations
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        operations.add_cancel(sub)
        args = parser.parse_args(['cancel', '--loop', 'l', '--pr', '3', '--seat', 'shadow'])
        self.assertEqual((args.pr, args.seat, args.func), (3, 'shadow', operations.cmd_cancel))


class Sandbox(unittest.TestCase):
    """The production stop: contained's wait kills the sandbox's process group (#1148)."""

    def test_a_set_stop_kills_the_process_group_and_raises_stopped(self):
        with tempfile.TemporaryDirectory() as home:
            pidfile = Path(home) / 'pid'
            argv = [sys.executable, '-c',
                    f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); "
                    "print('working', flush=True); time.sleep(60)"]
            stop = threading.Event()
            threading.Timer(0.5, stop.set).start()
            started = time.monotonic()
            with patch.object(contained, 'command', lambda **kw: argv), \
                    self.assertRaises(contained.Stopped) as caught:
                contained._run(timeout=60, stop=stop, home=home)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIn(b'working', caught.exception.output)
            self.assertFalse(alive(int(pidfile.read_text())))

    def test_without_stop_it_runs_to_completion(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(contained, 'command',
                              lambda **kw: [sys.executable, '-c', 'print("ok")']):
                result = contained._run(timeout=30, home=home)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'ok'))


class ProductionTurn(Base):
    """The worker's production path ends a stopped turn as cancelled and hands back a breach."""

    def test_a_stopped_production_turn_is_cancelled_and_hands_the_breach_back(self):
        sup = self.sup()
        sup.production_config = self.root / 'unused'
        sup.submit('d', REPO, 1, HEAD, 'adjudicator')
        breach = SimpleNamespace(resumed=[])
        breach.breach_resume = lambda pr, head, rounds: breach.resumed.append((pr, head, rounds))

        def turn(t):
            with sup._connect() as con:
                t.row = con.execute("SELECT * FROM runs WHERE id=?", (t.run_id,)).fetchone()
                con.execute("UPDATE runs SET state='running' WHERE id=?", (t.run_id,))
                con.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (t.run_id,))
            t.loop, t.breach = LOOP, (breach, 2)
            raise contained.Stopped()

        with patch.object(sup, '_claim_read_live', lambda row, read: None), \
                patch.object(sup, '_production_turn', turn), \
                patch.object(sup, 'failure_notice', lambda *a: None):
            sup._run_one()
        row = sup.get('d')
        self.assertEqual((row['state'], row['error']), ('cancelled', OPERATOR_STOPPED))
        self.assertEqual(breach.resumed, [(1, HEAD, 2)])

    def test_the_turn_is_not_launched_once_the_stop_is_set(self):
        # The check sits before the turn is counted and launched, and the launch carries the
        # stop event into the sandbox's wait (exercised in Sandbox above).
        source = Path(run_supervisor.turns.__file__).read_text()
        check = source.index("raise run_supervisor.TurnCancelled(run_supervisor.OPERATOR_STOPPED)")
        launch = source.index("turn.rc = trusted_turn.run_turn(")
        count = source.index("pacing.count_turn(loop['id']")
        self.assertLess(check, count)
        self.assertLess(count, launch)
        self.assertIn("stop=cancel)", source[launch:launch + 3000])


class Desktop(Base):
    """The Desktop's confirmed cancel goes through the same code path (#1148, every path #323)."""

    def setUp(self):
        super().setUp()
        for target, value in (('diaktoros.run_supervisor.production_ledger', lambda: self.db),
                              ('diaktoros.gh.pr', lambda loop, pr: {'head': {'sha': HEAD}})):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_confirmed_cancel_cancels_exactly_what_was_previewed(self):
        from diaktoros import one_shot_action
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        sup.submit('f', REPO, 1, HEAD, 'fixer')
        offer = one_shot_action.public_plan(one_shot_action.plan(LOOP, 'cancel', pr=1, seat='reviewer'))
        self.assertTrue(offer['eligible'], offer)
        self.assertIn('reviewer pending', offer['reason'])
        self.assertNotIn('snapshot', offer, 'the run ids never cross the page boundary')
        result = one_shot_action.confirmed_perform(LOOP, 'cancel', offer['confirmation'], pr=1,
                                                   seat='reviewer')
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual((sup.get('r')['state'], sup.get('f')['state']), ('cancelled', 'pending'))

    def test_a_run_that_appears_after_the_preview_makes_it_stale(self):
        from diaktoros import one_shot_action
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        offer = one_shot_action.plan(LOOP, 'cancel', pr=1)
        sup.submit('f', REPO, 1, HEAD, 'fixer')
        result = one_shot_action.confirmed_perform(LOOP, 'cancel', offer['confirmation'], pr=1)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual((sup.get('r')['state'], sup.get('f')['state']), ('pending', 'pending'))

    def test_a_running_turn_answers_partial_until_its_worker_stops_it(self):
        from diaktoros import one_shot_action
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        self.set_state('r', 'running')
        offer = one_shot_action.plan(LOOP, 'cancel', pr=1)
        result = one_shot_action.confirmed_perform(LOOP, 'cancel', offer['confirmation'], pr=1)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNotNone(sup.get('r')['cancel_requested'])

    def test_nothing_open_offers_nothing(self):
        from diaktoros import one_shot_action
        sup = self.sup()
        sup.submit('r', REPO, 1, HEAD, 'reviewer')
        self.set_state('r', 'succeeded')
        offer = one_shot_action.plan(LOOP, 'cancel', pr=1)
        self.assertEqual((offer['eligible'], offer['confirmation'], offer['reason']),
                         (False, '', 'nothing open to cancel'))

    def test_the_page_endpoint_validates_the_cancel_target(self):
        from diaktoros import desktop_api
        with patch.object(desktop_api, '_hooks_loop', return_value=LOOP):
            self.assertEqual(desktop_api._action_target('loop', 'cancel', 1, None), LOOP)
            self.assertEqual(desktop_api._action_target('loop', 'cancel', 1, 'shadow'), LOOP)
            for pr, seat in ((None, None), (0, None), (1, 'nobody')):
                with self.assertRaises(desktop_api.BadRequest):
                    desktop_api._action_target('loop', 'cancel', pr, seat)


if __name__ == '__main__':
    unittest.main()
