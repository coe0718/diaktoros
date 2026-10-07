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
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import _ci_green  # noqa: E402  CI reads as green unless a test says otherwise

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
start = time.time()
# The fixture leaves no evidence behind, and cannot: the per-turn home is a temp dir the launcher
# reclaims when the turn ends, and a writable /work is a sized tmpfs that no host process reads
# afterwards. What it does -- and what the tests assert -- is observable from outside the sandbox:
# the seat's argv as seen in /proc (including --run-budget and the marker-named grandchildren
# below), plus the result the worker reports. So there is nothing to write down in here.
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if spec['mode'] == 'overrun':
    # A grandchild in its own session that means to outlive the turn, and a light spinner.
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)',
                      spec['marker'] + '-escapee'], start_new_session=True, **quiet)
    subprocess.Popen([sys.executable, '-c',
                      'import time\nwhile True:\n    sum(range(2000)); time.sleep(0.02)',
                      spec['marker'] + '-spinner'], **quiet)
    while True:            # a hung tool call: never honours --run-budget
        time.sleep(0.5)
if spec['mode'] == 'push_then_hang':
    # A fixer's one push, sent early; the host takes its time publishing it, and the agent
    # (like one waiting on its tool call) overruns the budget meanwhile.
    with open('/work/src/lib.rs', 'w') as out:
        out.write('// fixed\n')
    subprocess.run([sys.executable, '-m', 'review_loop.broker_client', 'push', '--files',
                    'src/lib.rs', '--message', 'fix'], **quiet)
    while True:
        time.sleep(0.5)
write = subprocess.run([sys.executable, '-m', 'review_loop.broker_client', 'review',
                        '--verdict', 'APPROVE', '--body-file', '/work/review.txt'],
                       capture_output=True, text=True)
time.sleep(max(0.0, start + spec['finish_after'] - time.time()))
sys.exit(0 if write.returncode == 0 else 3)
'''


def driver(spec_path: str) -> None:
    """The worker's host side, in its own process: claim, launch, and report the ledger.

    ``seat`` is the turn under test (a reviewer, or an adjudicator ruling on a spent cap);
    with ``retry_budget`` the operator then re-arms it (`retry`) on that budget and the worker
    runs it again, so the second launch is observed too.
    """
    from types import SimpleNamespace
    from unittest import mock
    from review_loop import config, gh, safe_push, seat_model, trusted_fetch, trusted_turn
    from review_loop import state as state_mod
    from review_loop.run_supervisor import Supervisor, describe_run, write_evidence

    spec = json.loads(Path(spec_path).read_text())
    root, seat = Path(spec['root']), spec.get('seat', 'reviewer')
    loop = {'id': 'widgets', 'repo': 'acme/widgets', 'base': 'main', 'cap': 3,
            'state_dir': str(root / 'state'), 'fixers': ['fixer'], 'reviewers': ['reviewer'],
            'read_token': 'reader', 'turn_budget_s': spec['budget'],
            'unattended_fixer_push': seat == 'fixer',
            'reviewer_seat': 'reviewer', 'ttl_min': 45, 'inflight_ttl_min': 10,
            'tokens': {name: str(root / f'{name}.pat') for name in ('reader', 'reviewer', 'fixer')},
            'seats': {'reviewer': {'login': 'reviewer'}, 'fixer': {'login': 'fixer'}}}
    heads = {7: HEAD7, 8: HEAD8}
    def verdicts_for_fixer(seat):
        # A fixer turn needs the changes-requested verdict it answers, at the head.
        return [{'id': 1, 'state': 'CHANGES_REQUESTED', 'commit_id': HEAD7, 'body': 'fix it',
                 'submitted_at': '2026-09-21T00:00:00Z', 'user': {'id': 2, 'login': 'reviewer'}}
                ] if seat == 'fixer' else []

    # An adjudicator rules on a spent cap: three changes-requested verdicts at the head.
    verdicts = [{'id': n, 'state': 'CHANGES_REQUESTED', 'commit_id': HEAD7, 'body': 'no',
                 'submitted_at': f'2026-09-2{n}T00:00:00Z', 'user': {'id': 2, 'login': 'reviewer'}}
                for n in (1, 2, 3)] if seat == 'adjudicator' else verdicts_for_fixer(seat)

    pushes = []

    def slow_push(_loop, **kw):
        # The host side of the push: begin_push (the write-ahead intent) has been committed by
        # the broker; this is the GitHub part, which here outlasts the sandbox's kill.
        pushes.append({'at': time.monotonic()})
        time.sleep(spec.get('push_hold', 0))
        pushes[-1]['done'] = time.monotonic()
        return {'new_head': 'd' * 40}

    def pr(number):
        return {'number': number, 'state': 'open', 'draft': False, 'user': {'login': 'fixer'},
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
        if '/comments' in path:
            return []
        return pr(int(path.split('/pulls/')[1].split('/')[0]))

    inference = SimpleNamespace(upstream='http://127.0.0.1:9/v1/chat/completions', key='k',
                                model='fixture-model', api_mode='chat_completions',
                                credential_provider=lambda: None, proxy_model='',
                                client_identity='')
    # The ledger where `hermes dk status/explain` read it.
    sup = Supervisor(Path(os.environ['HERMES_HOME']) / 'state' / 'diaktoros-runs.sqlite',
                     production_config=root / 'runtime.json',
                     hermes_home=os.environ['HERMES_HOME'], lease_seconds=5,
                     capacity={'reviewer': 1, 'fixer': 1, 'adjudicator': 1})
    st = state_mod.state_for(loop)
    spawned = []
    trusted_turn.KILL_GRACE_S = spec['grace']
    with mock.patch.object(Supervisor, '_spawn', lambda self: spawned.append(time.time())), \
         mock.patch.object(config, 'by_repo', return_value=loop), \
         mock.patch.object(gh, 'api', side_effect=_ci_green.green(api)), \
         mock.patch.object(gh, 'reviews', return_value=verdicts), \
         mock.patch.object(gh, 'pr_files_read', return_value=([{
             'filename': 'src/lib.rs', 'status': 'modified', 'additions': 1, 'deletions': 0,
             'patch': '@@ -1 +1 @@\n-old\n+new'}], '')), \
         mock.patch.object(gh, 'issue_comments_read', return_value=([], '')), \
         mock.patch.object(trusted_fetch, 'stage', return_value=Path(spec['checkout'])), \
         mock.patch.object(seat_model, 'load_runtime', return_value=spec['runtime']), \
         mock.patch.object(seat_model, 'resolve_seat', return_value=inference), \
         mock.patch.object(safe_push, 'push', side_effect=slow_push):
        if seat == 'adjudicator':
            st.breach_set(7, {'pr': 7, 'head': HEAD7, 'rounds': 3,
                              'status': 'awaiting-adjudication', 'reason': 'cap spent'})
        # The turn under test, and a second reviewer turn waiting for the seat (capacity 1).
        sup.enqueue('turn-7', loop['repo'], 7, HEAD7, seat, budget=spec['budget'],
                    turn_key='breach:3' if seat == 'adjudicator' else '')
        sup.enqueue('turn-8', loop['repo'], 8, HEAD8, 'reviewer', budget=spec['budget'])
        spawned.clear()

        def run() -> dict:
            started = time.monotonic()
            sup._run_one()
            elapsed = time.monotonic() - started
            for push in pushes:
                push['at'], push['done'] = (round(push['at'] - started, 2),
                                            round(push.get('done', started) - started, 2))
            row = sup.get('turn-7')
            with sup._connect() as con:
                active = con.execute("SELECT COUNT(*) FROM runs WHERE seat=? AND state IN "
                                     "('claimed','launching','running','uncertain')",
                                     (seat,)).fetchone()[0]
            with sup._connect() as con:
                wrote = write_evidence(con, row['id'])
            return {'elapsed': round(elapsed, 2), 'state': row['state'], 'error': row['error'],
                    'write': wrote, 'pushes': list(pushes),
                    'push_intent': row['push_intent'], 'push_confirmed': row['push_confirmed'],
                    'outcome': row['outcome'], 'budget': row['budget'], 'pid': row['pid'],
                    'retries': row['retries'], 'retry_at': row['retry_at'],
                    'active_runs_after': active,
                    'marker': (st.breach_get(7) or {}).get('status')}

        first = run()
        notices = []
        sup.notify(notices.append)
        status = sup.status()
        described = [describe_run(row, loop['id']) for row in status]
        # The seat is free again: the waiting turn is claimable right now.
        claimed = sup._claim()
        result = {**first, 'worker_rearmed': len(spawned), 'writes': list(writes),
                  'next_claim_is_waiting_turn': bool(claimed) and claimed[0] == sup.get('turn-8')['id'],
                  'status': status, 'described': described, 'notices': notices}
        if spec.get('retry_budget'):
            result['retry'] = sup.retry(sup.get('turn-7')['id'], budget=spec['retry_budget'])
            result['retry_budget_on_row'] = sup.get('turn-7')['budget']
            result['second'] = run()
    print(json.dumps(result))


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

    def turn(self, mode: str, finish_after: float = 0.0, budget: int = BUDGET,
             seat: str = 'reviewer', retry_budget: int = 0,
             push_hold: float = 0) -> tuple[dict, dict, dict]:
        marker = f'RLBUDGET-{uuid.uuid4().hex[:12]}'
        self.marker = marker
        venv = self.root / f'venv-{mode}'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'bin/python').symlink_to(self.python)
        (venv / 'bin/hermes').write_text(FAKE_HERMES)
        (venv / 'behaviour.json').write_text(json.dumps(
            {'mode': mode, 'marker': marker, 'finish_after': finish_after}))
        checkout = self.root / f'checkout-{mode}'
        checkout.mkdir()
        (checkout / 'review.txt').write_text('checked offline')
        (checkout / 'src').mkdir()
        (checkout / 'src/lib.rs').write_text('// before\n')
        spec = self.root / f'spec-{mode}.json'
        spec.write_text(json.dumps({
            'root': str(self.root), 'budget': budget, 'grace': GRACE, 'checkout': str(checkout),
            'seat': seat, 'retry_budget': retry_budget, 'push_hold': push_hold,
            'runtime': {'source': str(self.source), 'venv': str(venv),
                        'runtime': str(self.python.parents[1]), 'rust': str(self.root / 'rust')}}))
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.home),
               'HERMES_HOME': str(self.home / '.hermes'), 'TMPDIR': self.tmpdir,
               'REVIEW_LOOP_CONFIG_DIR': str(self.root / 'no-config')}
        worker = subprocess.Popen([sys.executable, __file__, '--driver', str(spec)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        seen: dict = {}
        claims: list = []          # the loop's seat claims, as the worker writes them (#98)
        while worker.poll() is None:
            try:
                claims.append(json.loads((self.root / 'state' / 'locks.json').read_text()))
            except (OSError, ValueError):
                pass
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
        # The fixture cannot leave a file behind, by design: the per-turn home is a temp dir the
        # launcher reclaims when the turn ends, and a writable /work is a sized tmpfs that no host
        # process reads afterwards (that is the point of the size bound). So the evidence comes from
        # the surfaces the host really has -- the seat's own argv as seen in /proc, and the result the
        # worker just reported -- rather than from a write that isolation is built to keep local.
        driver = json.loads(out)
        # The loop's seat claims, as the worker wrote them during and after the turn (#98).
        driver['claims_during'] = [c for c in claims if c]
        try:
            driver['claims_after'] = json.loads((self.root / 'state' / 'locks.json').read_text())
        except (OSError, ValueError):
            driver['claims_after'] = {}
        seat = [cmd for cmd in seen.values() if '/opt/venv/bin/hermes' in cmd]
        handed = None
        for cmd in seat:
            parts = cmd.split()
            if '--run-budget' in parts:
                handed = int(parts[parts.index('--run-budget') + 1])
                break
        evidence = {
            'argv_run_budget': handed,
            'write_rc': 0 if driver['writes'] else 1,
            'finished_after': driver['elapsed'],
            'children': len([cmd for cmd in seen.values() if self.marker in cmd]),
            # When the seat's push was sent, from the worker's own result (the host rebases it to
            # the turn): the fixture writes nothing inside the sandbox, so this is the channel the
            # host really has, alongside the /proc argv above.
            'push_sent': (driver['pushes'] or [{}])[0].get('at'),
        }
        return driver, {cmd: 1 for cmd in seen.values()}, evidence

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
                         f'budget (sandbox stopped {GRACE}s past it) — raise turn_budget_s '
                         '(hermes dk set --loop widgets --reviewer-turn-budget N), '
                         'then `retry`')
        # #53 x #49: failed before any write, and not backed off for an automatic retry — the
        # same budget would run out again. It is re-armable (`retry`), which the text says.
        self.assertEqual((result['retries'], result['retry_at']), (0, None))
        self.assertEqual(result['writes'], [])
        # While it ran, its worker held the reviewer's seat claim with the run's own budget;
        # the kill released it (#98 review 4: the claim now has a production writer).
        held = [c['reviewer']['acme/widgets#7'] for c in result['claims_during']
                if 'acme/widgets#7' in c.get('reviewer', {})]
        self.assertTrue(held)
        self.assertEqual((held[-1]['head'], held[-1]['budget']), (HEAD7, BUDGET))
        self.assertNotIn('acme/widgets#7', result['claims_after'].get('reviewer', {}))
        # The seat is released: nothing active, the worker re-armed for the waiting turn, and
        # that turn is the next one claimed.
        self.assertEqual(result['active_runs_after'], 0)
        self.assertGreaterEqual(result['worker_rearmed'], 1)
        self.assertTrue(result['next_claim_is_waiting_turn'])
        # What the operator sees: the ledger status and the outbox notice name the budget.
        [status] = [row for row in result['status'] if row['pr'] == 7]
        self.assertEqual((status['state'], status['budget']), ('failed', BUDGET))
        [notice] = result['notices']
        self.assertIn(f'killed at the {BUDGET}s turn budget', notice)
        self.assertIn('seat=reviewer', notice)
        self.assertIn('No external write was made', notice)
        self.assertIn('hermes dk retry --loop widgets --pr 7 --seat reviewer', notice)
        # The line `hermes dk status` and `explain` print for it.
        [line] = [text for text in result['described'] if text.startswith('reviewer #7')]
        self.assertIn(f'killed at the {BUDGET}s turn budget', line)
        self.assertIn('raise turn_budget_s', line)
        self.assertIn(f'no external write — raise the turn budget (now {BUDGET}s): hermes '
                      'dk set --loop widgets --reviewer-turn-budget N, then re-arm: '
                      'hermes dk retry --loop widgets --pr 7 --seat reviewer', line)

    def test_a_seat_that_finishes_just_under_the_budget_completes(self):
        result, _, evidence = self.turn('finish', finish_after=BUDGET - 1)
        self.assertEqual(evidence['write_rc'], 0, evidence)
        self.assertGreaterEqual(evidence['finished_after'], BUDGET - 1)
        self.assertEqual((result['state'], result['outcome'], result['error']),
                         ('succeeded', 0, None))
        self.assertEqual(len(result['writes']), 1)
        self.assertEqual(result['notices'], [])
        self.assertEqual(result['active_runs_after'], 0)
        self.assertTrue(result['next_claim_is_waiting_turn'])

    def test_a_clean_stop_inside_the_grace_is_not_killed(self):
        # Real Hermes stops itself at --run-budget; its last step may land just past it. The
        # grace is there so that clean stop wins the race instead of a SIGKILL.
        result, _, evidence = self.turn('finish', finish_after=BUDGET + GRACE / 2)
        self.assertGreater(evidence['finished_after'], BUDGET)
        self.assertEqual((result['state'], result['error']), ('succeeded', None))

    def test_a_timed_out_adjudicator_hands_its_marker_back_and_retry_rules_again(self):
        # The breach marker goes 'adjudicating' just before launch. A budget kill made no
        # ruling, so the marker is handed back, and `retry` on a raised budget launches the
        # ruling again instead of being refused as "breach marker already claimed".
        result, seen, _ = self.turn('overrun', seat='adjudicator', retry_budget=BUDGET + 2)
        self.assertEqual((result['state'], result['retries'], result['writes']), ('failed', 0, []),
                         result['error'])
        self.assertIn(f'killed at the {BUDGET}s turn budget', result['error'])
        self.assertIn('--turn-budget N), then `retry`', result['error'])
        self.assertGreaterEqual(result['elapsed'], BUDGET + GRACE)
        self.assertEqual(result['marker'], 'awaiting-adjudication')
        self.assertEqual((result['retry'], result['retry_budget_on_row']), ('pending', BUDGET + 2))
        second = result['second']
        self.assertNotIn('breach marker', second['error'] or '')
        self.assertEqual(second['state'], 'failed')
        self.assertIn(f'killed at the {BUDGET + 2}s turn budget', second['error'])
        self.assertGreaterEqual(second['elapsed'], BUDGET + 2 + GRACE)
        self.assertEqual((second['marker'], second['active_runs_after']),
                         ('awaiting-adjudication', 0))
        # Both launches really ran in the sandbox, each handed its own budget.
        argv = [cmd for cmd in seen if cmd.startswith('/opt/venv/bin/python /opt/venv/bin/hermes')]
        self.assertEqual(sorted(cmd.split('--run-budget ')[1] for cmd in argv),
                         sorted([str(BUDGET), str(BUDGET + 2)]))

    def test_a_push_completed_in_the_drain_makes_the_budget_kill_final(self):
        # The fixer sends its push at once; the host's publish outlasts the kill (the drain
        # lets it finish), so the turn is killed at its budget *after* a completed write. That
        # run wrote: final, never replayed, and no "raise the budget, then retry".
        hold = BUDGET + GRACE + 3
        result, seen, evidence = self.turn('push_then_hang', seat='fixer', push_hold=hold)
        [push] = result['pushes']
        self.assertLess(push['at'], BUDGET)                          # sent inside the budget
        self.assertGreater(push['done'], BUDGET + GRACE)             # finished after the kill
        self.assertGreaterEqual(result['elapsed'], push['done'])     # the drain waited for it
        self.assertEqual((result['push_intent'], bool(result['push_confirmed'])), (None, True))
        self.assertEqual(result['write'], 'fixer push recorded')
        self.assertEqual((result['state'], result['retries'], result['retry_at']),
                         ('failed', 0, None))
        self.assertIn(f'killed at the {BUDGET}s turn budget', result['error'])
        self.assertIn('after it wrote (fixer push recorded)', result['error'])
        for wrong in ('raise turn_budget_s', 'then `retry`', 'no external write'):
            self.assertNotIn(wrong, result['error'])
        [line] = [text for text in result['described'] if text.startswith('fixer #7')]
        self.assertIn('may have written (fixer push recorded) — never replayed; a new head gets '
                      'a fresh turn', line)
        self.assertNotIn('re-arm', line)
        [notice] = result['notices']
        self.assertIn('Possible external write (fixer push recorded)', notice)
        self.assertNotIn('No external write', notice)
        self.assertEqual(evidence['argv_run_budget'], BUDGET)
        self.assertLess(evidence['push_sent'], BUDGET)

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
