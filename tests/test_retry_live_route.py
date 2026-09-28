"""Issue #53 end to end: webhook -> gate script -> run ledger -> detached worker -> turn.

The live route path, run the way the Hermes gateway runs it (``webhook_filters.run_route_script``):
``[sys.executable, script]`` with the raw webhook payload on stdin and ``cwd`` = the script's
directory. Everything after the gate is production code in its own processes: the gate's
``Supervisor.submit`` spawns the real ``_production-worker`` (its own stripped environment), which
claims the row, resolves the seat, stages the head from "GitHub", starts the inference proxy and
the write broker, and launches the turn.

Two fakes, nothing else:

* GitHub is one local HTTP world. The gate, CLI and watchdog reach it through the
  ``REVIEW_LOOP_GH_STUB`` executable; the worker (which production strips of that stub) through
  ``sitecustomize`` in a disposable copy of the plugin, which points ``gh.API`` at it.
* The same ``sitecustomize`` replaces only ``contained.run`` (the bwrap launcher) with a fake
  agent: a real child that calls the real inference proxy (whose upstream answers 429) or the
  real broker (which POSTs the review), or a launch that fails before any child exists.

No real Hermes, model, sandbox, GitHub, user HOME or credential is used. Set
``REVIEW_LOOP_TRANSCRIPT=/path`` to write the operator-visible transcript of each run.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from review_loop import ledger, run_supervisor
from review_loop.inference_proxy import PATH

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from worker_wait import wait_for_workers  # noqa: E402
HEAD = 'a' * 40
REPO = 'acme/widgets'

# The only seam: present solely in the disposable plugin copy the worker imports.
SITECUSTOMIZE = r'''
import errno, http.client, json, os, subprocess, sys
_home = os.environ.get('HOME') or ''
if _home and os.path.exists(os.path.join(_home, 'offline-port')):
    from review_loop import contained, gh
    gh.API = 'http://127.0.0.1:' + open(os.path.join(_home, 'offline-port')).read().strip()
    http.client.HTTPSConnection = http.client.HTTPConnection

    def _fake_bwrap(*, timeout=180, **kwargs):
        modes = json.load(open(os.path.join(_home, 'agent-modes.json')))
        launches = os.path.join(_home, 'agent-launches')
        n = len(open(launches).read().splitlines()) if os.path.exists(launches) else 0
        mode = modes[min(n, len(modes) - 1)]
        with open(launches, 'a') as out:
            out.write(mode + '\n')
        if mode == 'launch-failure':
            raise OSError(errno.EAGAIN, 'bwrap: clone(CLONE_NEWUSER) failed: '
                                        'Resource temporarily unavailable')
        argv = [sys.executable, os.path.join(_home, 'fake-agent.py'), mode,
                os.path.join(str(kwargs['inference_socket_dir']), 'model.sock'),
                os.path.join(str(kwargs['broker_socket_dir']), 'broker.sock')]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env={'PATH': '/usr/bin:/bin',
                                   'PYTHONPATH': os.path.dirname(os.path.abspath(__file__))})

    contained.run = _fake_bwrap
'''

# The "agent": talks only to the two capabilities a sandboxed turn is given.
FAKE_AGENT = r'''
import json, sys
from review_loop import broker_ipc
from review_loop.inference_proxy import PATH, _UnixHTTP
mode, model_sock, broker_sock = sys.argv[1:4]
print('hermes: review turn started', flush=True)
conn = _UnixHTTP(model_sock, timeout=30)
conn.request('POST', PATH, headers={'Content-Type': 'application/json'}, body=json.dumps(
    {'model': 'fixture-model', 'messages': [{'role': 'user', 'content': 'mode:' + mode}]}))
reply = conn.getresponse()
data = reply.read().decode()
if reply.status != 200:
    print(f'Error code: {reply.status} - {data}', file=sys.stderr)
    print('API call failed after provider error; giving up this turn', file=sys.stderr)
    sys.exit(1)
answer = broker_ipc.request('review', verdict='APPROVE', body='offline verified',
                            socket_path=broker_sock)
print(json.dumps(answer), flush=True)
if mode == 'write-then-crash':
    print('Traceback (most recent call last): agent crashed after its review', file=sys.stderr)
    sys.exit(1)
sys.exit(0 if answer.get('ok') else 1)
'''

GH_STUB = r'''
import http.client, json, os, sys
port = int(open(os.path.join(os.environ['HOME'], 'offline-port')).read())
conn = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
conn.request(os.environ.get('GH_METHOD', 'GET'), sys.argv[1],
             body=sys.argv[2] if len(sys.argv) > 2 else None,
             headers={'Authorization': 'token dummy-reader', 'X-Gh-Stub': '1',
                      'Content-Type': 'application/json'})
reply = conn.getresponse()
data = reply.read().decode()
if reply.status == 404:
    print('null')               # gh._stub: "no such resource", as a real 404 reads
elif reply.status >= 300:
    print(f'HTTP {reply.status}', file=sys.stderr)
    sys.exit(1)
else:
    print(data)
'''


class World(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, data, content_type='application/json'):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        world = self.server.world
        url = urlsplit(self.path)
        page = int(parse_qs(url.query).get('page', ['1'])[0])
        path = url.path
        base = '/repos/' + REPO
        world['gets'].append((path, bool(self.headers.get('X-Gh-Stub'))))
        if path == '/user':
            login = self.headers.get('Authorization', '').split()[-1].removeprefix('dummy-')
            self._send(200, {'login': login, 'id': {'reader': 1, 'reviewer': 2, 'fixer': 3}.get(login)})
        elif path == base + '/pulls/7':
            self._send(200, world['pr'])
        elif path == base + '/pulls':
            self._send(200, [world['pr']] if page == 1 and world['pr']['state'] == 'open' else [])
        elif path == base + '/pulls/7/reviews':
            self._send(200, world['reviews'] if page == 1 else [])
        elif path == base + '/pulls/7/files':
            # The change under review (#50): one modified file, one page.
            self._send(200, [{'filename': name, 'status': 'modified', 'additions': 1,
                              'deletions': 0, 'patch': '@@ -0,0 +1 @@\n+x'}
                             for name in list(world['blobs'])[:1]] if page == 1 else [])
        elif path.startswith(base + '/pulls/7/reviews/'):
            rid = int(path.rsplit('/', 1)[-1])
            found = [r for r in world['reviews'] if r['id'] == rid]
            self._send(200 if found else 404, found[0] if found else {})
        elif path == base + '/hooks':
            self._send(200, [{'id': 1, 'active': True, 'config': {'url': 'http://gw/webhooks/widgets-review'}},
                             {'id': 2, 'active': True, 'config': {'url': 'http://gw/webhooks/widgets-fix'}}]
                       if page == 1 else [])
        elif path == base + '/git/commits/' + HEAD:
            self._send(200, {'sha': HEAD, 'tree': {'sha': world['tree_sha']}})
        elif path == base + '/git/trees/' + world['tree_sha']:
            self._send(200, {'sha': world['tree_sha'], 'truncated': False, 'tree': [
                {'path': name, 'type': 'blob', 'mode': '100644', 'sha': oid, 'size': len(raw)}
                for name, (oid, raw) in world['blobs'].items()]})
        elif path.startswith(base + '/git/blobs/'):
            oid = path.rsplit('/', 1)[-1]
            raw = next((raw for blob, raw in world['blobs'].values() if blob == oid), None)
            self._send(200 if raw is not None else 404, raw if raw is not None else b'')
        elif path.endswith('/comments') or path.endswith('/commits') or path.endswith('/timeline'):
            self._send(200, [])
        else:
            world['unknown'].append(self.path)
            self._send(404, {'message': 'Not Found'})

    def do_POST(self):
        world = self.server.world
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == PATH:
            content = json.dumps(data.get('messages'))
            world['model'].append(content)
            if 'mode:429' in content:
                self._send(429, {'error': {'type': 'rate_limit_error',
                                           'message': 'Rate limit reached for fixture-model; '
                                                      'retry after 20s'}})
            else:
                self._send(200, {'id': 'x', 'object': 'chat.completion', 'model': 'fixture-model',
                                 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                                     'role': 'assistant', 'content': 'Looks right.'}}]})
        elif self.path == f'/repos/{REPO}/pulls/7/reviews':
            rid = len(world['reviews']) + 1
            world['reviews'].append({
                'id': rid, 'commit_id': data['commit_id'], 'body': data['body'],
                'state': {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED'}[data['event']],
                'submitted_at': '2026-09-26T12:00:%02dZ' % rid,
                'user': {'id': 2, 'login': 'reviewer'}})
            self._send(200, {'id': rid, 'state': data['event']})
        else:
            world['unknown'].append('POST ' + self.path)
            self._send(404, {})


class LiveRouteRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Every detached worker a route spawned exits before the temp dir goes (see worker_wait).
        self.addCleanup(wait_for_workers, self.root)
        self.home = home = self.root / 'home'
        home.mkdir(mode=0o700)
        (home / 'review-loops.d').mkdir()
        # The plugin as installed: a copy of the package, with the seam only in this copy.
        self.plugin = home / 'plugins' / 'hermes-review-loop'
        self.plugin.mkdir(parents=True)
        for name in ('review_loop', 'scripts'):
            shutil.copytree(ROOT / name, self.plugin / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        (self.plugin / 'sitecustomize.py').write_text(SITECUSTOMIZE)
        (home / 'fake-agent.py').write_text(FAKE_AGENT)
        stub = self.root / 'gh-stub'
        stub.write_text('#!' + sys.executable + '\n' + GH_STUB)
        stub.chmod(0o700)
        # A committed tree for the turn's code snapshot (never executed: the launcher is fake).
        source = self.root / 'hermes-source'
        source.mkdir()
        (source / 'run_agent.py').write_text('print("fixture")\n')
        git = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1'}
        for args in (['init', '-q'], ['add', '.'],
                     ['-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                      'commit', '-qm', 'fixture']):
            subprocess.run(['git', *args], cwd=source, env=git, check=True,
                           capture_output=True, timeout=30)
        for name in ('venv', 'runtime', 'rust'):
            (self.root / name).mkdir()
        key = self.root / 'model.key'
        key.write_text('offline-fixture-model-key')
        key.chmod(0o600)
        tokens = {}
        for login in ('reader', 'reviewer', 'fixer'):
            path = self.root / (login + '.pat')
            path.write_text('dummy-' + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = {'id': 'widgets', 'repo': REPO, 'base': 'main', 'cap': 3,
                     'fixers': ['dev'], 'reviewers': ['reviewer'], 'reviewer_seat': 'reviewer',
                     'seats': {'reviewer': {'profile': 'fixture', 'route': 'widgets-review',
                                            'login': 'reviewer'},
                               'fixer': {'profile': 'fixture', 'route': 'widgets-fix',
                                         'login': 'fixer'}},
                     'read_token': 'reader', 'tokens': tokens, 'state_dir': str(home / 'state')}
        (home / 'review-loops.d/widgets.json').write_text(json.dumps(self.loop))
        blobs = {'Cargo.toml': b'[package]\nname="probe"\nversion="0.1.0"\nedition="2021"\n',
                 'src/lib.rs': b'#[test] fn works() {}\n'}
        self.world = {
            'pr': {'number': 7, 'state': 'open', 'draft': False, 'user': {'login': 'dev'},
                   'html_url': f'https://github.com/{REPO}/pull/7',
                   'base': {'ref': 'main', 'sha': 'b' * 40, 'repo': {'full_name': REPO}},
                   'head': {'sha': HEAD, 'ref': 'fix-7', 'repo': {'full_name': REPO}}},
            'tree_sha': 'c' * 40, 'reviews': [], 'model': [], 'gets': [], 'unknown': [],
            'blobs': {name: (hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest(), raw)
                      for name, raw in blobs.items()}}
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), World)
        self.server.world = self.world
        port = self.server.server_port
        (home / 'offline-port').write_text(str(port))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        runtime = {'source': str(source), 'venv': str(self.root / 'venv'),
                   'runtime': str(self.root / 'runtime'), 'rust': str(self.root / 'rust'),
                   'seats': {'reviewer': {'model': 'fixture-model', 'key_file': str(key),
                                          'upstream': f'https://127.0.0.1:{port}{PATH}'}}}
        (home / 'review-loop-runtime.json').write_text(json.dumps(runtime))
        (home / 'review-loop-runtime.json').chmod(0o600)
        # What the gateway hands a route script: its own (scrubbed) environment, no PYTHONPATH.
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'HERMES_HOME': str(home),
                    'REVIEW_LOOP_GH_STUB': str(stub), 'PYTHONDONTWRITEBYTECODE': '1',
                    'TMPDIR': tempfile.gettempdir()}
        self.db = home / 'state' / 'review-loop-runs.sqlite'
        self.transcript = []

    def tearDown(self):
        # A detached worker may still be exiting; let it finish before the tree is removed.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self._active():
            time.sleep(0.2)
        path = os.environ.get('REVIEW_LOOP_TRANSCRIPT')
        if path:
            with open(path, 'a') as out:
                out.write(f'\n######## {self.id()}\n' + '\n'.join(self.transcript) + '\n')

    # -- the operator's and the gateway's views --------------------------------------------

    def say(self, title, text=''):
        text = str(text).replace(str(self.root), '$TMP').rstrip()
        self.transcript.append(f'\n$ {title}' + (f'\n{text}' if text else ''))

    def modes(self, *modes):
        (self.home / 'agent-modes.json').write_text(json.dumps(modes))

    def launches(self):
        path = self.home / 'agent-launches'
        return path.read_text().split() if path.exists() else []

    def webhook(self, action):
        """The gateway's ``run_route_script``: the interpreter, the script, stdin, cwd."""
        script = self.plugin / 'scripts' / 'gate_reviewer.py'
        payload = {'action': action, 'number': 7, 'pull_request': self.world['pr'],
                   'repository': {'full_name': REPO}, 'sender': {'login': 'dev'}}
        result = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                                capture_output=True, text=True, env=self.env,
                                cwd=str(script.parent), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[SILENT]', result.stderr)
        self.say(f'gateway: pull_request.{action} -> gate_reviewer.py',
                 'stdout: [SILENT]\n' + result.stderr)
        return result.stderr

    def cli(self, command, **args):
        """``hermes review-loop COMMAND`` — the plugin's handler, without launching Hermes."""
        code = ('import argparse, sys\nsys.path.insert(0, %r)\nfrom review_loop import cli\n'
                'sys.exit(cli.cmd_%s(argparse.Namespace(**%r)))' % (str(self.plugin), command, args))
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                                env=self.env, cwd=str(self.plugin), timeout=60)
        flags = ' '.join(f'--{k} {v}' for k, v in args.items() if v is not None)
        self.say(f'hermes review-loop {command} {flags}', result.stdout + result.stderr)
        return result

    def supervisor_status(self):
        result = subprocess.run([sys.executable, '-m', 'review_loop.run_supervisor', 'status',
                                 str(self.db)], capture_output=True, text=True, env=self.env,
                                cwd=str(self.plugin), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)   # failed, waiting and uncertain runs only
        shown = [{k: row.get(k) for k in ('state', 'retries', 'outcome', 'error', 'write',
                                          'detail')} for row in rows]
        self.say('python -m review_loop.run_supervisor status DB', json.dumps(shown, indent=1))
        return rows[0] if rows else None

    def watchdog(self):
        """The cron job: the shim ``init`` writes runs the plugin's watchdog."""
        result = subprocess.run([sys.executable, str(self.plugin / 'scripts' / 'watchdog.py')],
                                capture_output=True, text=True, env=self.env,
                                cwd=str(self.plugin / 'scripts'), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.say('cron: review-loop watchdog (stdout is what the operator is sent)',
                 result.stdout or '(no output)')
        return result.stdout

    def row(self):
        with ledger.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute('SELECT * FROM runs')]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _active(self):
        if not self.db.exists():
            return False
        with ledger.connect(self.db) as con:
            return con.execute("SELECT COUNT(*) FROM runs WHERE state IN "
                               "('claimed','launching','running')").fetchone()[0] > 0

    def settle(self, states, launches=None, seconds=90):
        """Wait for the detached worker: the row in ``states``, and ``launches`` turns so far."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.db.exists():
                row = self.row()
                if (row['state'] in states and not self._active()
                        and (launches is None or len(self.launches()) == launches)):
                    return row
            time.sleep(0.2)
        self.fail(f'worker did not settle in {states}: {self.row()} launches={self.launches()}')

    def elapse(self):
        """The clock passes the backoff the worker recorded (nothing else is touched)."""
        row = self.row()
        self.assertEqual(row['state'], 'waiting')
        self.assertAlmostEqual(row['retry_at'] - row['updated'],
                               run_supervisor.backoff(row['retries']), delta=5)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET retry_at=? WHERE state='waiting'", (time.time() - 1,))
        self.say(f"(clock: retry {row['retries']} backoff of "
                 f"{int(run_supervisor.backoff(row['retries']))}s elapses)")

    # -- the scenarios -----------------------------------------------------------------------

    def test_pre_write_failures_wait_show_reason_and_retry_to_success(self):
        self.modes('429', 'launch-failure', '429', '429', 'approve')

        # 1. The webhook arrives; the real worker runs the turn; the provider answers 429.
        log = self.webhook('opened')
        self.assertIn('reviewer enqueued for isolated worker', log)
        row = self.settle({'waiting'}, launches=1)
        self.assertEqual((row['retries'], row['outcome'], row['error']),
                         (1, 1, 'turn exited with status 1'))
        self.assertIn('Error code: 429', row['detail'])
        self.assertIn('Rate limit reached for fixture-model', row['detail'])
        self.assertEqual(self.world['reviews'], [])
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM review_receipts').fetchone()[0], 0)

        # The operator sees the real reason, not an exception type.
        status = self.supervisor_status()
        self.assertEqual(status['state'], 'waiting')
        self.assertIn('Rate limit reached', status['detail'])
        shown = self.cli('status', loop='widgets')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('waiting', shown.stdout)
        self.assertIn('turn exited with status 1', shown.stdout)
        explained = self.cli('explain', loop='widgets', pr=7)
        self.assertIn('turn exited with status 1', explained.stdout)
        self.assertIn('Rate limit reached for fixture-model', explained.stdout)
        self.assertIn('blocked:    isolated reviewer turn waiting at this head — turn exited '
                      'with status 1', explained.stdout)
        self.assertIn('next:       attempt 2 of 4 due in', explained.stdout)
        self.assertNotIn('no guard is holding this PR back', explained.stdout)
        # Waiting is not an alarm: the cron sweep says nothing and launches nothing early.
        self.assertNotIn('Review-loop worker', self.watchdog())
        self.assertEqual(len(self.launches()), 1)

        # 2. A redelivery while it waits schedules nothing, and the gate says so (#73).
        log = self.webhook('opened')
        self.assertIn('not enqueued: duplicate waiting (retry 1 due in', log)
        self.assertNotIn('enqueued for isolated worker', log)
        self.assertEqual(len(self.launches()), 1)

        # 3. Automatic retry: once the backoff elapses, the armed cron sweep relaunches it.
        #    The sandbox then fails to launch at all — still pre-write, still retryable.
        self.elapse()
        self.watchdog()
        row = self.settle({'waiting'}, launches=2)
        self.assertEqual(row['retries'], 2)
        self.assertIn('isolated turn failed: BlockingIOError: [Errno 11]', row['error'])
        self.assertIn('bwrap: clone(CLONE_NEWUSER) failed', row['error'])
        explained = self.cli('explain', loop='widgets', pr=7)
        self.assertIn('bwrap: clone(CLONE_NEWUSER) failed', explained.stdout)

        # Two more 429s exhaust the automatic budget: failed, with the reason and output tail.
        for launches in (3, 4):
            self.elapse()
            self.watchdog()
            row = self.settle({'waiting', 'failed'}, launches=launches)
        self.assertEqual((row['state'], row['retries']), ('failed', run_supervisor.MAX_RETRIES))
        self.assertEqual(row['error'], f'retry limit ({run_supervisor.MAX_RETRIES} attempts): '
                                       'turn exited with status 1')
        notice = self.watchdog()
        self.assertIn('Review-loop worker failed', notice)
        self.assertIn('retry limit (4 attempts): turn exited with status 1', notice)
        self.assertIn('Rate limit reached for fixture-model', notice)
        self.assertIn('No external write was made (4 failed attempts on record)', notice)
        self.assertIn('hermes review-loop retry --loop widgets --pr 7 --seat reviewer', notice)
        explained = self.cli('explain', loop='widgets', pr=7)
        self.assertIn('blocked:    isolated reviewer turn failed at this head — retry limit',
                      explained.stdout)
        self.assertIn('next:       no external write — re-arm: hermes review-loop retry '
                      '--loop widgets --pr 7 --seat reviewer', explained.stdout)
        self.assertNotIn('Review-loop worker', self.watchdog(), 'one notice per failure')
        self.assertEqual(len(self.launches()), 4)

        # 4. The PR goes back to draft before the operator retries: the worker's claim reads
        #    the draft and waits for ready (no launch), rather than failing or reviewing it.
        self.world['pr']['draft'] = True
        mark = len(self.world['gets'])
        retried = self.cli('retry', loop='widgets', pr=7, seat='reviewer')
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn('re-armed (was failed: retry limit (4 attempts)', retried.stdout)
        self.assertIn('worker started', retried.stdout)
        deadline = time.monotonic() + 60
        while not any(path == f'/repos/{REPO}/pulls/7' and not stub
                      for path, stub in self.world['gets'][mark:]):
            self.assertLess(time.monotonic(), deadline, 'the worker never read the PR')
            time.sleep(0.2)
        row = self.settle({'pending'})
        self.assertEqual((row['retries'], len(self.launches())), (0, 4))
        self.say('(worker claim read the PR as draft: run stays pending, nothing launched)')

        # 5. ready_for_review re-drives the same pending run; this time the turn succeeds.
        self.world['pr']['draft'] = False
        log = self.webhook('ready_for_review')
        self.assertIn('reviewer pending: isolated worker re-armed', log)
        row = self.settle({'succeeded'}, launches=5)
        self.assertIsNone(row['error'])
        self.assertEqual([r['state'] for r in self.world['reviews']], ['APPROVED'])
        self.assertEqual(self.launches(), ['429', 'launch-failure', '429', '429', 'approve'])
        self.assertIsNone(self.supervisor_status(), 'nothing left failed or waiting')
        self.assertEqual(self.world['unknown'], [], 'every GitHub read was one the world knows')

    def test_post_write_failure_stays_final_and_is_never_replayed(self):
        self.modes('write-then-crash')
        self.webhook('opened')
        row = self.settle({'failed', 'uncertain', 'waiting'}, launches=1)
        # The review reached GitHub before the agent died: final, not retried.
        self.assertEqual((row['state'], row['retries']), ('failed', 0))
        self.assertIsNone(row['retry_at'])
        self.assertEqual(row['error'], 'turn exited with status 1')
        self.assertIn('agent crashed after its review', row['detail'])
        self.assertEqual([r['state'] for r in self.world['reviews']], ['APPROVED'])
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT state FROM review_receipts').fetchall(),
                             [('confirmed',)])
        status = self.supervisor_status()
        self.assertEqual(status['state'], 'failed')
        self.assertIn('review receipt', status['write'])

        # A redelivery does not re-arm it, and says why.
        log = self.webhook('opened')
        self.assertTrue('duplicate failed' in log or "already has a reviewer's verdict" in log, log)
        self.assertNotIn('re-armed', log)
        # The operator's retry refuses it, with the reconcile instructions.
        refused = self.cli('retry', loop='widgets', pr=7, seat='reviewer')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('refused: ', refused.stdout)
        self.assertIn('never replayed', refused.stdout)
        self.assertIn('reconcile', refused.stdout)
        # The cron sweep neither relaunches it nor reports it as write-free.
        notice = self.watchdog()
        self.assertIn('Review-loop worker failed', notice)
        self.assertIn('Possible external write', notice)
        self.assertNotIn('No external write was made', notice)
        # explain: the run is final and its review is on GitHub; it holds nothing.
        explained = self.cli('explain', loop='widgets', pr=7)
        self.assertIn('may have written (review receipt confirmed) — never replayed',
                      explained.stdout)
        self.assertNotIn('isolated reviewer turn failed at this head', explained.stdout)
        self.watchdog()
        time.sleep(1)
        self.assertEqual(self.row()['state'], 'failed')
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(len(self.world['reviews']), 1)


if __name__ == '__main__':
    unittest.main()
