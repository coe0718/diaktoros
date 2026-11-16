"""Real-Mac guarded-turn disposal soak; no credentials or live model services.

A failure retains storage and writes evidence. This is a containment/storage
fixture, not 200 Hermes model turns or a physical reboot acceptance test.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import select
import subprocess
import sys
import tempfile
import time
import traceback
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diaktoros import contained, native_cleanup, native_lifecycle, native_recovery, native_storage, seatbelt

MODES = ('fsync', 'setsid', 'double_fork', 'build_server', 'idle_daemon')
SEAT = r'''
import ctypes, errno, json, os, sys, time
from pathlib import Path
mode = sys.argv[1]
work = Path.cwd()
children = []
if mode == 'fsync':
    with (work/'payload').open('wb') as out:
        out.write(b'x'*1024*1024); out.flush(); os.fsync(out.fileno())
else:
    child = os.fork()
    if child == 0:
        if mode == 'double_fork':
            if os.fork() != 0: os._exit(0)
        if mode == 'setsid':
            for escape in (os.setsid, lambda: os.setpgid(0,0)):
                try: escape()
                except PermissionError: pass
                else:
                    (work/'escape').write_text('detach succeeded'); os._exit(31)
            libc=ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype=ctypes.c_long
            for number,args in ((147,()),(82,(0,0))):
                ctypes.set_errno(0)
                if libc.syscall(ctypes.c_long(number), *[ctypes.c_long(x) for x in args]) >= 0 or ctypes.get_errno() not in (errno.EPERM,errno.EACCES):
                    (work/'escape').write_text('raw detach was not denied'); os._exit(32)
        holder = (work/'server-cache').open('wb') if mode == 'build_server' else None
        if holder: holder.write(b'build-server fixture'); holder.flush()
        (work/'child.tmp').write_text(str(os.getpid())); (work/'child.tmp').replace(work/'child')
        deadline=time.monotonic()+60
        while time.monotonic()<deadline: time.sleep(.02)
        os._exit(0)
    if mode == 'double_fork': os.waitpid(child,0)
    deadline=time.monotonic()+10
    while not (work/'child').exists() and time.monotonic()<deadline:
        if (work/'escape').exists(): raise SystemExit(33)
        time.sleep(.01)
    children=[int((work/'child').read_text())]
(work/'ready.tmp').write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp(),'children':children,'mode':mode}))
(work/'ready.tmp').replace(work/'ready')
deadline=time.monotonic()+10
while not (work/'go').exists() and time.monotonic()<deadline: time.sleep(.01)
if not (work/'go').exists(): raise SystemExit(34)
raise SystemExit(7)
'''


class AcceptanceError(RuntimeError):
    pass


class PendingBudget:
    """Measure every eligible image against total sweeps and monotonic time."""
    def __init__(self):
        self.pending = {}
        self.worst_sweeps = 0
        self.worst_seconds = 0.0

    def add(self, root, now):
        self.pending.setdefault(str(root), {'since': now, 'sweeps': 0})

    def begin_sweep(self, now):
        for value in self.pending.values():
            if now - value['since'] >= 300 or value['sweeps'] >= 5:
                raise AcceptanceError('pending image exceeded five sweeps or five minutes')
            value['sweeps'] += 1

    def complete_sweep(self, now, exists):
        for root, value in list(self.pending.items()):
            elapsed = now - value['since']
            if not exists(root):
                if elapsed > 300:
                    raise AcceptanceError('image disappeared after five-minute deadline')
                self.worst_sweeps = max(self.worst_sweeps, value['sweeps'])
                self.worst_seconds = max(self.worst_seconds, elapsed)
                del self.pending[root]
            elif elapsed >= 300 or value['sweeps'] >= 5:
                raise AcceptanceError('image still present at disposal deadline')


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as out:
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_exits(expected, observed, members):
    if not expected or expected != observed or members:
        raise AcceptanceError(f'process completion unverified: missing={sorted(expected-observed)}, group_members={sorted(members)}')


class Soak:
    def __init__(self, output, turns):
        self.output = output.resolve()
        self.output.mkdir(mode=0o700, parents=True, exist_ok=True)
        native_recovery._private(self.output.lstat(), directory=True)
        control = Path(tempfile.mkdtemp(prefix='dk-soak-control-', dir=self.output.parent)).resolve()
        self.pool = native_cleanup.Manager(control)
        self.boot_id = native_recovery.boot_identity()
        self.turns = turns
        self.completed = 0
        self.budget = PendingBudget()
        self.injected_deferrals = 0
        self.observed_deferrals = 0
        self.max_pending = 0
        self.sweep_number = 0

    def event(self, kind, **value):
        record = {'kind': kind, 'monotonic': time.monotonic(), **value}
        with (self.output/'events.jsonl').open('a') as out:
            out.write(json.dumps(record, sort_keys=True)+'\n')
            out.flush(); os.fsync(out.fileno())
        print('NATIVE_CLEANUP_SOAK '+json.dumps(record, sort_keys=True), flush=True)

    def sweep(self):
        self.budget.begin_sweep(time.monotonic())
        self.sweep_number += 1
        # Actual fresh-process recovery, not reused Python workspace objects.
        source = str(Path(native_cleanup.__file__).resolve().parent.parent)
        code = ('import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'from diaktoros.native_cleanup import Manager; notices=[]; '
                'result=Manager(Path(sys.argv[2]),create=False).sweep(notify=notices.append); '
                'result["notices"]=notices; print(json.dumps(result))')
        try:
            result = subprocess.run([sys.executable,'-I','-B','-c',code,source,str(self.pool.root)],
                                    capture_output=True,text=True,timeout=90,check=False,
                                    env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
        except subprocess.TimeoutExpired as error:
            for suffix, raw in (('stdout',error.stdout),('stderr',error.stderr)):
                value = raw.decode(errors='replace') if isinstance(raw,bytes) else raw or ''
                (self.output/f'sweep-{self.sweep_number:04}.{suffix}').write_text(value[-250000:])
            raise AcceptanceError('fresh cleanup worker exceeded 90 seconds; retain storage') from error
        (self.output/f'sweep-{self.sweep_number:04}.stderr').write_text(result.stderr[-250000:])
        (self.output/f'sweep-{self.sweep_number:04}.stdout').write_text(result.stdout[-250000:])
        if result.returncode:
            raise AcceptanceError(f'fresh cleanup worker failed: {result.returncode}; {result.stderr[-2000:]}')
        value = json.loads(result.stdout)
        self.budget.complete_sweep(time.monotonic(), lambda p: Path(p).exists())
        self.event('sweep', sweep=self.sweep_number, **value)

    def turn(self, cycle):
        self.sweep()
        while True:
            try:
                workspace = native_storage.Workspace(64, cleanup_manager=self.pool).__enter__()
                break
            except native_cleanup.AdmissionRefused as error:
                self.event('admission-refused', cycle=cycle, error=str(error))
                if not self.budget.pending:
                    raise
                self.sweep()
        root = workspace.root
        mode = MODES[cycle % len(MODES)]
        interpreter = Path(sys.executable).resolve()
        framework = Path(sys.base_prefix)/'Resources/Python.app/Contents/MacOS/Python'
        if framework.is_file(): interpreter = framework.resolve()
        policy = seatbelt.profile(read_roots=(Path(sys.base_prefix),), write_roots=(workspace.work,))
        policy = replace(policy,text=policy.text+'(allow syscall-unix)\n(deny syscall-unix (syscall-number 82 147 244))\n')
        policy_path = self.output/f'policy-{cycle:04}.sb'
        policy_path.write_text(policy.text)
        native_recovery.arm(workspace)
        with native_recovery.Record(root) as receipt:
            turn_id = receipt.read()['turn_id']
        expected = set()
        group = None
        with closing(select.kqueue()) as events:
            original = contained.capture_process
            def release(guardian, **kwargs):
                nonlocal group, expected
                deadline = time.monotonic()+10
                ready = workspace.work/'ready'
                while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)
                value = json.loads(ready.read_text())
                pids = [value['pid'], *value['children']]
                if (value['mode'] != mode or len(pids)>3 or len(set(pids)) != len(pids) or
                        any(type(p) is not int or p<=1 for p in pids)):
                    raise AcceptanceError('invalid ready identities')
                group = os.getpgid(value['pid'])
                if group != value['group'] or any(os.getpgid(p)!=group for p in pids):
                    raise AcceptanceError('fixture escaped the reserved group')
                expected = set(pids)
                for pid in pids:
                    events.control([select.kevent(pid,filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD|select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)],0,0)
                (workspace.work/'go').touch()
                return original(guardian, **kwargs)
            try:
                with mock.patch.object(contained,'capture_process',side_effect=release):
                    result = native_lifecycle.capture(policy.command(policy_path,[str(interpreter),'-I','-B','-c',SEAT,mode]),
                                                      env={'PATH':'/usr/bin:/bin'},timeout=20,
                                                      cwd=workspace.work,recovery_root=root)
                observed = set()
                deadline = time.monotonic()+5
                while observed != expected and time.monotonic()<deadline:
                    observed.update(e.ident for e in events.control(None,8,max(0,deadline-time.monotonic()))
                                    if e.filter==select.KQ_FILTER_PROC)
                require_exits(expected, observed, native_lifecycle._group_members(group))
                if result.returncode != 7:
                    raise AcceptanceError(f'turn result changed: {result.returncode}')
                with native_recovery.Record(root) as receipt:
                    record = receipt.read()
                    if record['state']!='drained' or record['turn_id']!=turn_id:
                        raise AcceptanceError('durable completion receipt mismatch')
                # Record the result durably outside the image before releasing its lease.
                atomic_json(self.output/f'turn-{cycle:04}.json', {'cycle':cycle,'mode':mode,
                            'returncode':result.returncode,'turn_id':turn_id,'processes':sorted(expected),
                            'kernel_exits':sorted(observed),'group_survivors':0})
                eligible_at = time.monotonic()
                injected = cycle % 10 == 0
                if injected:
                    with mock.patch.object(native_storage.Workspace,'_detach_owned',
                                           side_effect=native_storage.StorageError('injected busy eject for queue exercise')):
                        workspace._cleanup()
                else:
                    workspace._cleanup()
                if root.exists():
                    if workspace.pending_cleanup is None:
                        raise AcceptanceError('retained image lacks pending disposal acknowledgement')
                    self.budget.add(root, eligible_at)
                    if injected: self.injected_deferrals += 1
                    else: self.observed_deferrals += 1
                self.completed += 1
                summary = self.pool.status()
                self.max_pending = max(self.max_pending,summary['pending_images'])
                self.event('turn',cycle=cycle,mode=mode,returncode=7,turn_id=turn_id,
                           kernel_exits=len(observed),group_survivors=0,injected=injected,
                           pending=workspace.pending_cleanup,summary=summary)
            except BaseException:
                workspace.retain('soak failed; retain evidence and storage')
                # Release host descriptors, but never delete or signal a discovered PID.
                if workspace._disposal_lease is not None:
                    os.close(workspace._disposal_lease); workspace._disposal_lease=None
                raise

    def snapshot(self):
        control = self.pool.root/native_recovery.JOURNAL
        if control.is_file():
            (self.output/'cleanup-control.json').write_bytes(control.read_bytes()[:native_recovery.MAX_RECORD+1])
        receipts = self.output/'receipts'
        receipts.mkdir(mode=0o700,exist_ok=True)
        for root in self.pool.volumes.iterdir():
            if root.is_symlink(): continue
            path=root/native_recovery.JOURNAL
            if path.is_file():
                (receipts/(root.name+'.json')).write_bytes(path.read_bytes()[:native_recovery.MAX_RECORD+1])
        for name,argv in (('attachments',['/usr/bin/hdiutil','info','-plist']),
                          ('processes',['/bin/ps','-axo','pid,ppid,pgid,state,time,command'])):
            try:
                result=subprocess.run(argv,capture_output=True,text=True,timeout=10)
                (self.output/(name+'.txt')).write_text((result.stdout+result.stderr)[-250000:])
            except Exception as error:
                (self.output/(name+'.txt')).write_text(str(error))

    def run(self):
        error = None
        started = time.monotonic()
        try:
            for cycle in range(self.turns): self.turn(cycle)
            while self.budget.pending: self.sweep()
            if list(self.pool.volumes.iterdir()):
                raise AcceptanceError('owned workspaces remain after final reconciliation')
        except BaseException as exception:
            error = f'{type(exception).__name__}: {exception}'
            (self.output/'failure.txt').write_text(traceback.format_exc())
        finally:
            self.snapshot()
            summary = {'passed':error is None,'requested_turns':self.turns,'completed_turns':self.completed,
                       'architecture':platform.machine(),'boot_id':self.boot_id,
                       'seconds':round(time.monotonic()-started,3),'error':error,
                       'injected_deferrals':self.injected_deferrals,'observed_deferrals':self.observed_deferrals,
                       'max_pending_images':self.max_pending,'remaining_pending':len(self.budget.pending),
                       'worst_cleanup_sweeps':self.budget.worst_sweeps,
                       'worst_cleanup_seconds':round(self.budget.worst_seconds,3),
                       'pool_root':str(self.pool.root),'physical_reboot_tested':False,
                       'hermes_model_turns':False}
            atomic_json(self.output/'result.json',summary)
            self.event('result',**summary)
        return 0 if error is None else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns',type=int,default=200)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.turns<=200: parser.error('turn count must be 1..200')
    reason=seatbelt.unavailable()
    if reason: parser.error(reason)
    return Soak(args.output,args.turns).run()


if __name__=='__main__':
    raise SystemExit(main())
