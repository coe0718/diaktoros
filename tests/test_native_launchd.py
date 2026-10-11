"""Characterize OS-owned group teardown; not a production launchd backend.

All survivors are bounded/cooperative fixtures. Signals target a unique launchd
service label, never a discovered PID. Kernel exits precede fixture removal.
"""
import _home_guard  # noqa: F401
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from diaktoros import native_lifecycle, seatbelt

SEAT = '''
import os, time
from pathlib import Path
held = os.open('held-open', os.O_CREAT | os.O_WRONLY, 0o600)
os.write(held, b'owned work')
for fd in (0, 1, 2): os.close(fd)
factory = os.fork()
if factory == 0:
    if os.fork() != 0: os._exit(0)
    Path('descendant').write_text(str(os.getpid()))
    deadline = time.monotonic() + 25
    while not Path('stop').exists() and time.monotonic() < deadline: time.sleep(0.02)
    os._exit(0)
os.waitpid(factory, 0)
while not Path('descendant').exists(): time.sleep(0.01)
Path('ready').write_text('ready')
deadline = time.monotonic() + 25
while not Path('stop').exists() and time.monotonic() < deadline: time.sleep(0.02)
'''

JOB = '''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
config = json.loads((root / 'config.json').read_text())
assert os.getpid() == os.getpgrp(), 'launchd job is not its group leader'
(root / 'job-start.tmp').write_text(json.dumps({'leader':os.getpid(), 'group':os.getpgrp()}))
(root / 'job-start.tmp').rename(root / 'job-start.json')
child = subprocess.Popen(config['argv'], cwd=root / 'work', env={'PATH':'/usr/bin:/bin'},
                         close_fds=True, start_new_session=False)
deadline = time.monotonic() + 10
while not (root / 'work/ready').exists() and time.monotonic() < deadline: time.sleep(0.02)
assert (root / 'work/ready').exists(), 'seat never became ready'
data = {'leader':os.getpid(), 'group':os.getpgrp(), 'seat':child.pid,
        'descendant':int((root / 'work/descendant').read_text())}
(root / 'job-ready.tmp').write_text(json.dumps(data))
(root / 'job-ready.tmp').rename(root / 'job-ready.json')
deadline = time.monotonic() + 20
while not (root / 'exit-job').exists() and time.monotonic() < deadline: time.sleep(0.02)
# Deliberately no child wait, signals or Python cleanup. launchd owns teardown.
os._exit(0)
'''


class NativeLaunchdOwnership(unittest.TestCase):
    def setUp(self):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_LAUNCHD') == '1':
                self.fail(reason)
            self.skipTest(reason)

    def launchctl(self, *args, check=True):
        result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True,
                                timeout=10, env={'PATH': '/usr/bin:/bin'})
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def exits(self, queue, wanted, timeout):
        found = set()
        deadline = time.monotonic() + timeout
        while found != wanted and time.monotonic() < deadline:
            ready = queue.control(None, len(wanted), max(0, deadline - time.monotonic()))
            found.update(e.ident for e in ready if e.filter == select.KQ_FILTER_PROC)
        return found

    def probe(self, mode, *, abandon=False):
        root = Path(tempfile.mkdtemp(prefix='dk-launchd-', dir='/tmp')).resolve()
        with closing(select.kqueue()) as events:
            work = root / 'work'
            work.mkdir(mode=0o700)
            interpreter = Path(sys.executable).resolve()
            framework = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
            if framework.is_file():
                interpreter = framework.resolve()
            profile = seatbelt.profile(read_roots=(Path(sys.base_prefix),), write_roots=(work,))
            policy = root / 'seat.sb'
            policy.write_text(profile.text + '(allow syscall-unix)\n'
                              '(deny syscall-unix (syscall-number 82 147 244))\n')
            (root / 'config.json').write_text(json.dumps({
                'argv': profile.command(policy, [str(interpreter), '-I', '-B', '-c', SEAT])}))
            script = root / 'job.py'
            script.write_text(JOB)
            label = 'dev.diaktoros.probe.' + uuid.uuid4().hex
            domain = f'user/{os.getuid()}'
            service = domain + '/' + label
            plist = root / 'job.plist'
            plist.write_bytes(plistlib.dumps({
                'Label': label, 'ProgramArguments': [str(interpreter), '-I', '-B', str(script), str(root)],
                'RunAtLoad': True, 'KeepAlive': False, 'AbandonProcessGroup': abandon,
                'StandardOutPath': str(root / 'stdout'), 'StandardErrorPath': str(root / 'stderr')}))
            record = None
            try:
                self.launchctl('bootstrap', domain, str(plist))
                deadline = time.monotonic() + 12
                while not (root / 'job-ready.json').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / 'job-ready.json').exists(),
                                (root / 'stderr').read_text() if (root / 'stderr').exists() else
                                self.launchctl('print', service, check=False).stderr)
                record = json.loads((root / 'job-ready.json').read_text())
                group = record['group']
                children = {record['seat'], record['descendant']}
                self.assertEqual(record['leader'], group)
                self.assertTrue(children <= native_lifecycle._group_members(group))
                events.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                              fflags=select.KQ_NOTE_EXIT)
                                for pid in children | {record['leader']}], 0, 0)
                if mode == 'kill':
                    self.launchctl('kill', 'SIGKILL', service)
                else:
                    (root / 'exit-job').touch()
                if abandon:
                    self.assertEqual(self.exits(events, {record['leader']}, 5), {record['leader']})
                    self.assertTrue(children <= native_lifecycle._group_members(group))
                    (work / 'stop').touch()
                    self.assertEqual(self.exits(events, children, 5), children)
                else:
                    wanted = children | {record['leader']}
                    self.assertEqual(self.exits(events, wanted, 8), wanted,
                                     'launchd left original-group processes alive')
                deadline = time.monotonic() + 5
                while native_lifecycle._group_members(group) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(native_lifecycle._group_members(group))
                print(f'NATIVE_LAUNCHD mode={mode} abandon={abandon} group_survivors=0', flush=True)
            finally:
                # These known cooperative fixtures have a hard deadline even
                # when bootstrap or the characterization assertion fails.
                (work / 'stop').touch()
                try:
                    self.launchctl('bootout', service, check=False)
                finally:
                    if record is None and (root / 'job-start.json').exists():
                        record = json.loads((root / 'job-start.json').read_text())
                if record is not None:
                    deadline = time.monotonic() + 30
                    while native_lifecycle._group_members(record['group']) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(native_lifecycle._group_members(record['group']),
                                     f'fixture still owns {root}')
                self.assertNotEqual(self.launchctl('print', service, check=False).returncode, 0,
                                    'temporary launchd service was not removed')
                # A missing start receipt cannot prove that a launched job had
                # no descendants. Retain its fixtures rather than deleting them.
                self.assertTrue(record is not None,
                                f'no job identity receipt; retained {root}')
                shutil.rmtree(root)

    def test_launchd_drains_group_after_job_sigkill(self):
        self.probe('kill')

    def test_launchd_drains_group_after_normal_job_exit(self):
        self.probe('exit')

    def test_abandon_control_keeps_children_until_cooperative_stop(self):
        self.probe('exit', abandon=True)


if __name__ == '__main__':
    unittest.main()
