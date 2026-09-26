"""#49, observed: a seat that really runs long inside the real sandbox is killed at its budget.

``test_turn_budget`` follows the budget from the gate to the argv with bwrap faked. This file
runs the rest for real: the production worker's own ``_run_one`` → ``_run_production`` →
``trusted_turn.run_turn`` → ``contained.run`` → **bubblewrap**, with the host broker and the
inference capability live on their sockets. Only the agent is fake: ``/opt/venv/bin/hermes`` is
a small script (never the real Hermes, never a model) that either overruns — ignoring its
``--run-budget`` like a hung tool call, and spawning a spinner plus a grandchild in its own
session that tries to outlive the turn — or finishes its one scoped write and exits in time.

Each case runs in a child process (the worker's host side with GitHub, the seat's model
resolution and the PR export stubbed), while this process watches the sandbox's host process
tree through ``/proc``: every process seen under the turn's bwrap must be gone afterwards.

The budgets are seconds, not the 60 s minimum a loop may configure (the ledger row takes any
positive budget), and the kill grace is shortened to match, so the whole file runs in ~30 s.
``REVIEW_LOOP_SANDBOX_BUDGET``/``REVIEW_LOOP_SANDBOX_GRACE`` override both for a longer demo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HEAD7, HEAD8 = 'a' * 40, 'c' * 40
BUDGET = int(os.environ.get('REVIEW_LOOP_SANDBOX_BUDGET') or 6)
GRACE = int(os.environ.get('REVIEW_LOOP_SANDBOX_GRACE') or 4)

# The fake agent. It runs inside the sandbox as `/opt/venv/bin/python /opt/venv/bin/hermes chat
# ... --run-budget N` under the real inference bridge, exactly where Hermes would.
FAKE_HERMES = r'''
import json, subprocess, sys, time
spec = json.load(open('/opt/venv/behaviour.json'))
budget = int(sys.argv[sys.argv.index('--run-budget') + 1])
start = time.time()
evidence = {'mode': spec['mode'], 'argv_run_budget': budget}
def save(**kw):
    evidence.update(kw)
    with open('/work/evidence.json', 'w') as out:
        json.dump(evidence, out)
save()
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if spec['mode'] == 'overrun':
    # A grandchild in its own session that means to outlive the turn, and a light spinner.
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)',
                      spec['marker'] + '-escapee'], start_new_session=True, **quiet)
    subprocess.Popen([sys.executable, '-c',
                      'import time\nwhile True:\n    sum(range(2000)); time.sleep(0.02)',
                      spec['marker'] + '-spinner'], **quiet)
    save(children=2)
    while True:            # a hung tool call: never honours --run-budget
        time.sleep(0.5)
write = subprocess.run([sys.executable, '-m', 'review_loop.broker_client', 'review',
                        '--verdict', 'APPROVE', '--body-file', '/work/review.txt'],
                       capture_output=True, text=True)
save(write_rc=write.returncode, write_out=write.stdout[-400:])
time.sleep(max(0.0, start + spec['finish_after'] - time.time()))
save(finished_after=round(time.time() - start, 2))
sys.exit(0 if write.returncode == 0 else 3)
'''


def driver(spec_path: str) -> None:
    """The worker's host side, in its own process: claim, launch, and report the ledger."""
    from types import SimpleNamespace
    from unittest import mock
    from review_loop import config, gh, seat_model, trusted_fetch, trusted_turn
    from review_loop.run_supervisor import Supervisor

    spec = json.loads(Path(spec_path).read_text())
    root = Path(spec['root'])
    loop = {'id': 'widgets', 'repo': 'acme/widgets', 'base': 'main', 'cap': 3,
            'state_dir': str(root / 'state'), 'fixers': ['fixer'], 'reviewers': ['reviewer'],
            'read_token': 'reader', 'turn_budget_s': spec['budget'],
            'tokens': {name: str(root / f'{name}.pat') for name in ('reader', 'reviewer', 'fixer')},
            'seats': {'reviewer': {'login': 'reviewer'}, 'fixer': {'login': 'fixer'}}}
    heads = {7: HEAD7, 8: HEAD8}

    def pr(number):
        return {'number': number, 'state': 'open', 'draft': False,
                'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': loop['repo']}},
                'head': {'sha': heads[number], 'ref': f'fix-{number}',
                         'repo': {'full_name': loop['repo']}}}

    writes = []

    def api(_loop, path, method='GET', body=None, login=None):
        if path == '/user':
            return {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}[login]}
        if method == 'POST':
            writes.append(path)
            return {'id': 42}
        if path.endswith('/reviews/42'):
            return {'id': 42, 'state': 'APPROVED', 'commit_id': HEAD7,
                    'user': {'id': 2, 'login': 'reviewer'}}
        return pr(int(path.split('/pulls/')[1].split('/')[0]))

    inference = SimpleNamespace(upstream='http://127.0.0.1:9/v1/chat/completions', key='k',
                                model='fixture-model', api_mode='chat_completions',
                                credential_provider=lambda: None, proxy_model='',
                                client_identity='')
    sup = Supervisor(root / 'runs.sqlite', production_config=root / 'runtime.json',
                     hermes_home=os.environ['HERMES_HOME'], lease_seconds=5,
                     capacity={'reviewer': 1, 'fixer': 1, 'adjudicator': 1})
    spawned = []
    trusted_turn.KILL_GRACE_S = spec['grace']
    with mock.patch.object(Supervisor, '_spawn', lambda self: spawned.append(time.time())), \
         mock.patch.object(config, 'by_repo', return_value=loop), \
         mock.patch.object(gh, 'api', side_effect=api), \
         mock.patch.object(gh, 'reviews', return_value=[]), \
         mock.patch.object(trusted_fetch, 'stage', return_value=Path(spec['checkout'])), \
         mock.patch.object(seat_model, 'load_runtime', return_value=spec['runtime']), \
         mock.patch.object(seat_model, 'resolve_seat', return_value=inference):
        # The turn under test, and a second reviewer turn waiting for the seat (capacity 1).
        sup.enqueue('turn-7', loop['repo'], 7, HEAD7, 'reviewer', budget=spec['budget'])
        sup.enqueue('turn-8', loop['repo'], 8, HEAD8, 'reviewer', budget=spec['budget'])
        spawned.clear()
        started = time.monotonic()
        sup._run_one()
        elapsed = time.monotonic() - started
        row = sup.get('turn-7')
        with sup._connect() as con:
            active = con.execute("SELECT COUNT(*) FROM runs WHERE seat='reviewer' AND state IN "
                                 "('claimed','launching','running','uncertain')").fetchone()[0]
        notices = []
        sup.notify(notices.append)
        # The seat is free again: the waiting turn is claimable right now.
        claimed = sup._claim()
    print(json.dumps({
        'elapsed': round(elapsed, 2), 'state': row['state'], 'error': row['error'],
        'outcome': row['outcome'], 'budget': row['budget'], 'pid': row['pid'],
        'active_reviewer_runs_after': active, 'worker_rearmed': len(spawned),
        'next_claim_is_waiting_turn': bool(claimed) and claimed[0] == sup.get('turn-8')['id'],
        'writes': writes, 'status': sup.status(), 'notices': notices}))


def _proc(pid: int):
    """``(ppid, starttime, cmdline, state)`` from /proc, or None once the process is gone."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(
            errors='replace').strip()
    except OSError:
        return None
    fields = stat.rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19], cmdline, fields[0]


def _tree(tag: str) -> dict:
    """Every live process at or under a bwrap whose argv carries ``tag``, keyed by pid."""
    procs = {}
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            info = _proc(int(entry))
            if info:
                procs[int(entry)] = info
    roots = {pid for pid, info in procs.items() if 'bwrap' in info[2] and tag in info[2]}
    found, frontier = {}, list(roots)
    while frontier:
        pid = frontier.pop()
        found[pid] = procs[pid]
        frontier.extend(child for child, info in procs.items()
                        if info[0] == pid and child not in found)
    return found


@unittest.skipUnless(shutil.which('bwrap') and sys.platform.startswith('linux'),
                     'bubblewrap unavailable')
class SandboxedTurnBudget(unittest.TestCase):
    """The real kill path of #49, observed against a seat that actually runs long."""

    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(['bwrap', '--unshare-all', '--ro-bind', '/', '/', 'true'],
                               capture_output=True)
        if probe.returncode != 0:
            raise unittest.SkipTest('bubblewrap cannot create namespaces here')

    def setUp(self):
        base = tempfile.mkdtemp(prefix='rl-budget-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.root = Path(base)
        self.home = self.root / 'home'
        (self.home / '.hermes').mkdir(parents=True)
        for name in ('reader', 'reviewer', 'fixer'):
            (self.root / f'{name}.pat').write_text('dummy-' + name)
        (self.root / 'runtime.json').write_text('{}')
        (self.root / 'runtime.json').chmod(0o600)
        # The Hermes source snapshot: trusted_turn exports committed blobs only.
        source = self.root / 'source'
        source.mkdir()
        (source / 'run_agent.py').write_text('# placeholder: the fake agent is in the venv\n')
        git = ['git', '-C', str(source), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false']
        for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'snapshot']):
            subprocess.run(git + args, check=True, capture_output=True,
                           env={'PATH': '/usr/bin:/bin', 'HOME': str(self.home)})
        self.source = source
        self.python = Path(sys.executable).resolve()
        (self.root / 'rust').mkdir()
        # The turn's socket directory must stay short (AF_UNIX path limit).
        self.tmpdir = (tempfile.gettempdir() if len(tempfile.gettempdir()) <= 30 else '/tmp')

    def turn(self, mode: str, finish_after: float = 0.0,
             budget: int = BUDGET) -> tuple[dict, dict, dict]:
        marker = f'RLBUDGET-{uuid.uuid4().hex[:12]}'
        venv = self.root / f'venv-{mode}'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/python').symlink_to(self.python)
        (venv / 'bin/hermes').write_text(FAKE_HERMES)
        (venv / 'behaviour.json').write_text(json.dumps(
            {'mode': mode, 'marker': marker, 'finish_after': finish_after}))
        checkout = self.root / f'checkout-{mode}'
        checkout.mkdir()
        (checkout / 'review.txt').write_text('checked offline')
        spec = self.root / f'spec-{mode}.json'
        spec.write_text(json.dumps({
            'root': str(self.root), 'budget': budget, 'grace': GRACE, 'checkout': str(checkout),
            'runtime': {'source': str(self.source), 'venv': str(venv),
                        'runtime': str(self.python.parents[1]), 'rust': str(self.root / 'rust')}}))
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home),
               'HERMES_HOME': str(self.home / '.hermes'), 'TMPDIR': self.tmpdir,
               'REVIEW_LOOP_CONFIG_DIR': str(self.root / 'no-config')}
        worker = subprocess.Popen([sys.executable, __file__, '--driver', str(spec)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        seen: dict = {}
        while worker.poll() is None:
            # bwrap's argv names this test's private root, so no other sandbox is mistaken for it.
            for pid, info in _tree(str(self.root)).items():
                seen.setdefault((pid, info[1]), info[2])
            time.sleep(0.2)
        out, err = worker.communicate()
        self.assertEqual(worker.returncode, 0, err[-3000:])
        # Whatever ran under the sandbox is gone: none of the seen processes (pid + start time,
        # so a reused pid is not mistaken for a survivor), and nothing carrying the marker.
        survivors: list = []
        for _ in range(25):
            survivors = [cmd for (pid, start), cmd in seen.items()
                         if (_proc(pid) or (None, None))[1] == start]
            survivors += [info[2] for pid, info in
                          ((p, _proc(int(p))) for p in os.listdir('/proc') if p.isdigit())
                          if info and marker in info[2]]
            if not survivors:
                break
            time.sleep(0.2)
        self.assertEqual(survivors, [], 'sandbox processes outlived the turn')
        evidence = json.loads((checkout / 'evidence.json').read_text())
        return json.loads(out), {cmd: 1 for cmd in seen.values()}, evidence

    def test_a_seat_that_overruns_is_killed_with_its_whole_tree_and_releases_the_seat(self):
        result, seen, evidence = self.turn('overrun')
        # The agent was handed the budget as Hermes's --run-budget, and ignored it.
        self.assertEqual(evidence['argv_run_budget'], BUDGET)
        self.assertEqual(evidence.get('children'), 2)
        # The tree really was there while it ran: bridge, agent, spinner, escapee.
        text = '\n'.join(seen)
        for part in ('review_loop.inference_proxy bridge', '/opt/venv/bin/hermes',
                     '-spinner', '-escapee'):
            self.assertIn(part, text)
        # Killed at budget + grace, not before and not much after.
        self.assertGreaterEqual(result['elapsed'], BUDGET + GRACE)
        self.assertLess(result['elapsed'], BUDGET + GRACE + 8)
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['outcome'])
        self.assertEqual(result['error'],
                         f'isolated turn failed: TimeoutExpired — killed at the {BUDGET}s turn '
                         f'budget (sandbox stopped {GRACE}s past it; raise turn_budget_s)')
        self.assertEqual(result['writes'], [])
        # The seat is released: nothing active, the worker re-armed for the waiting turn, and
        # that turn is the next one claimed.
        self.assertEqual(result['active_reviewer_runs_after'], 0)
        self.assertGreaterEqual(result['worker_rearmed'], 1)
        self.assertTrue(result['next_claim_is_waiting_turn'])
        # What the operator sees: the ledger status and the outbox notice name the budget.
        [status] = [row for row in result['status'] if row['pr'] == 7]
        self.assertEqual((status['state'], status['budget']), ('failed', BUDGET))
        [notice] = result['notices']
        self.assertIn(f'killed at the {BUDGET}s turn budget', notice)
        self.assertIn('seat=reviewer', notice)

    def test_a_seat_that_finishes_just_under_the_budget_completes(self):
        result, _, evidence = self.turn('finish', finish_after=BUDGET - 1)
        self.assertEqual(evidence['write_rc'], 0, evidence)
        self.assertGreaterEqual(evidence['finished_after'], BUDGET - 1)
        self.assertEqual((result['state'], result['outcome'], result['error']),
                         ('succeeded', 0, None))
        self.assertEqual(len(result['writes']), 1)
        self.assertEqual(result['notices'], [])
        self.assertEqual(result['active_reviewer_runs_after'], 0)
        self.assertTrue(result['next_claim_is_waiting_turn'])

    def test_a_clean_stop_inside_the_grace_is_not_killed(self):
        # Real Hermes stops itself at --run-budget; its last step may land just past it. The
        # grace is there so that clean stop wins the race instead of a SIGKILL.
        result, _, evidence = self.turn('finish', finish_after=BUDGET + GRACE / 2)
        self.assertGreater(evidence['finished_after'], BUDGET)
        self.assertEqual((result['state'], result['error']), ('succeeded', None))

    def test_the_ceiling_budget_reaches_the_sandbox_unclamped(self):
        # The largest budget a loop may set (4 h) is carried whole into the real sandbox's
        # argv and deadline; nothing on the way (worker default, lease, selector) caps it.
        from review_loop import config
        ceiling = config.TURN_BUDGET_RANGE[1]
        result, _, evidence = self.turn('finish', finish_after=1, budget=ceiling)
        self.assertEqual(evidence['argv_run_budget'], ceiling)
        self.assertEqual((result['state'], result['budget']), ('succeeded', ceiling))


if __name__ == '__main__':
    if sys.argv[1:2] == ['--driver']:
        driver(sys.argv[2])
    else:
        unittest.main()
