"""Characterize unresolved native lifecycle gaps; these are not production acceptance.

Every survivor is a cooperative, time-bounded fixture. The host registers a
kernel exit notification before triggering failure and waits for actual exit
before removing fixture paths. No polling process-tree killer is proposed here.
"""
import _home_guard  # noqa: F401
from pathlib import Path
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from diaktoros import seatbelt


CHILD = '''
import json, os, sys, time
from pathlib import Path
work, control, secret = map(Path, sys.argv[1:4])
if sys.argv[4] == 'detach':
    pid = os.fork()
    if pid:
        # The host's timeout will kill this original process group.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (control / 'release').exists():
            time.sleep(0.02)
        os.waitpid(pid, 0)
        raise SystemExit(0)
    os.setsid()
# Close capture pipes: surviving because a pipe is open is a different case.
fd = os.open('/dev/null', os.O_RDWR)
for target in (0, 1, 2):
    os.dup2(fd, target)
if fd > 2:
    os.close(fd)
(work / 'ready-tmp').write_text(json.dumps({'pid': os.getpid(), 'pgrp': os.getpgrp(),
                                             'parent': os.getppid()}))
(work / 'ready-tmp').replace(work / 'ready')
deadline = time.monotonic() + 15
try:
    while time.monotonic() < deadline and not (control / 'release').exists():
        if (control / 'probe').exists():
            try:
                secret.read_text()
            except PermissionError:
                (work / 'response-tmp').write_text('alive; host secret denied')
                (work / 'response-tmp').replace(work / 'after-failure')
            else:
                (work / 'response-tmp').write_text('HOST SECRET WAS READ')
                (work / 'response-tmp').replace(work / 'after-failure')
        time.sleep(0.02)
finally:
    (work / 'done').write_text('fixture exited')
'''

SUPERVISOR = '''
import json, subprocess, sys
from pathlib import Path
from diaktoros import contained
argv, env = json.loads(sys.argv[1]), json.loads(sys.argv[2])
try:
    contained.capture(argv, env=env, timeout=int(sys.argv[3]))
except subprocess.TimeoutExpired:
    Path(sys.argv[4]).write_text('timeout observed')
else:
    raise AssertionError('fixture unexpectedly completed')
'''


class NativeLifecycleGaps(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_LIFECYCLE') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def wait_file(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file() and path.stat().st_size:
                return path.read_text()
            time.sleep(0.02)
        self.fail(f'fixture did not write {path.name}')

    def probe_gap(self, *, detached):
        with tempfile.TemporaryDirectory(prefix='dk-life-', dir='/tmp') as directory:
            root = Path(directory).resolve()
            work, control = root / 'work', root / 'control'
            work.mkdir()
            control.mkdir()
            secret = root / 'host-secret'
            secret.write_text('FAKE_SECRET')
            script = control / 'child.py'
            script.write_text(CHILD)
            profile = seatbelt.profile(read_roots=(Path(sys.base_prefix), control),
                                       write_roots=(work,))
            policy = root / 'policy.sb'
            policy.write_text(profile.text)
            argv = profile.command(policy, [str(Path(sys.executable).resolve()), '-I', '-B',
                                            str(script), str(work), str(control), str(secret),
                                            'detach' if detached else 'stay'])
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(work), 'TMPDIR': str(work)}
            with select.kqueue() as exits:
                supervisor = subprocess.Popen(
                    [sys.executable, '-c', SUPERVISOR, json.dumps(argv), json.dumps(env),
                     '3' if detached else '30', str(root / 'timeout')],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True)
                registered = False
                try:
                    ready = json.loads(self.wait_file(work / 'ready'))
                    watched = [ready['pid'], ready['parent']] if detached else [ready['pid']]
                    exits.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                 fflags=select.KQ_NOTE_EXIT) for pid in watched], 0, 0)
                    registered = True
                    if detached:
                        self.assertEqual(ready['pid'], ready['pgrp'])
                        _, errors = supervisor.communicate(timeout=8)
                        self.assertEqual(supervisor.returncode, 0, errors.decode(errors='replace'))
                        self.assertEqual(self.wait_file(root / 'timeout'), 'timeout observed')
                    else:
                        supervisor.kill()  # SIGKILL: no Python finally/atexit can run.
                        supervisor.communicate(timeout=5)
                        self.assertEqual(supervisor.returncode, -signal.SIGKILL)
                    # Require fresh activity after the host failure, not an earlier heartbeat.
                    (control / 'probe').touch()
                    self.assertEqual(self.wait_file(work / 'after-failure'),
                                     'alive; host secret denied')
                finally:
                    (control / 'release').touch()
                    if supervisor.poll() is None:
                        supervisor.kill()
                    supervisor.communicate(timeout=5)
                    if registered:
                        remaining = set(watched)
                        deadline = time.monotonic() + 17
                        while remaining and time.monotonic() < deadline:
                            events = exits.control(None, len(remaining),
                                                   max(0, deadline - time.monotonic()))
                            for event in events:
                                self.assertTrue(event.fflags & select.KQ_NOTE_EXIT)
                                remaining.discard(event.ident)
                        self.assertFalse(remaining, 'fixture processes did not actually exit')
                    else:
                        # Startup failure may occur after fork but before ready. A bounded
                        # fixture observes release or its own deadline; preserve its paths.
                        time.sleep(16)

    def test_detached_child_survives_group_timeout_but_keeps_seatbelt(self):
        self.probe_gap(detached=True)

    def test_child_survives_supervisor_sigkill_but_keeps_seatbelt(self):
        self.probe_gap(detached=False)


if __name__ == '__main__':
    unittest.main()
