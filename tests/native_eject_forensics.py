"""Bounded, opt-in Mac eject forensics; never a production cleanup policy."""
import _home_guard  # noqa: F401
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import threading
import time
from unittest import mock

from diaktoros import native_storage, seatbelt

PREDICATE = ('process == "diskarbitrationd" OR process BEGINSWITH "diskimage" '
             'OR process == "fseventsd" OR process BEGINSWITH "mdworker" '
             'OR process == "mds" OR subsystem CONTAINS[c] "apfs"')
MAX_OUTPUT = 2 * 1024 * 1024
MAX_STREAM = 16 * 1024 * 1024


def holders(raw):
    """Parse lsof machine fields for read-only sampling, never signalling."""
    result, pid = {}, None
    for line in raw.splitlines():
        if line.startswith('p'):
            pid = int(line[1:]) if line[1:].isdigit() and int(line[1:]) > 1 else None
        elif pid is not None and line.startswith('c'):
            result[pid] = line[1:]
    return result


class Recorder:
    def __init__(self, output, *, verbose=False):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.verbose = verbose
        self.sequence = 0
        self.first_busy = False
        self.original_run = native_storage._run

    def emit(self, event):
        event = dict(event, utc=datetime.now(timezone.utc).isoformat())
        with (self.output / 'events.jsonl').open('a') as target:
            target.write(json.dumps(event) + '\n')
        print('NATIVE_EJECT_FORENSICS ' + json.dumps(event), flush=True)

    def command(self, tag, argv, *, timeout=5):
        self.sequence += 1
        prefix = f'{self.sequence:04d}-{tag}'
        started = time.monotonic()
        event = {'tag': tag, 'argv': argv, 'prefix': prefix}
        try:
            result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
            stdout, stderr = result.stdout, result.stderr
            event['returncode'] = result.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or b'', error.stderr or b''
            event['error'] = 'timeout'
        except OSError as error:
            stdout, stderr = b'', str(error).encode()
            event['error'] = str(error)
        for suffix, raw in (('stdout', stdout), ('stderr', stderr)):
            (self.output / f'{prefix}.{suffix}').write_bytes(raw[-MAX_OUTPUT:])
            event[suffix + '_bytes'] = len(raw)
            event[suffix + '_truncated'] = len(raw) > MAX_OUTPUT
        event['elapsed_seconds'] = round(time.monotonic() - started, 3)
        self.emit(event)
        return event, stdout, stderr

    def run(self, argv):
        if argv[0] != native_storage.HDIUTIL or argv[1] not in ('create', 'attach', 'detach'):
            return self.original_run(argv)
        actual = (argv[:-1] + ['-verbose', argv[-1]]
                  if self.verbose and argv[1] == 'detach' else argv)
        event, stdout, stderr = self.command(argv[1], actual, timeout=120)
        if event.get('error') or event['returncode']:
            raise native_storage.StorageError('hdiutil failed: ' +
                                             (stdout + stderr).decode(errors='replace')[-2000:])
        return stdout

    def snapshot(self, workspace, tag):
        self.emit({'tag': tag, 'root': str(workspace.root), 'image': str(workspace.image)})
        try:
            attachments = workspace._attachments()
        except Exception as error:
            self.emit({'tag': tag + '-ownership', 'error': str(error)})
            return
        # Raw hdiutil metadata can include the disk-image helper's process ID.
        (self.output / f'{tag}-attachments.json').write_text(json.dumps(attachments, default=str))
        devices = sorted({e['dev-entry'] for a in attachments for e in a.get('system-entities', [])
                          if re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*', e.get('dev-entry', ''))})
        if len(devices) > 32:
            self.emit({'tag': tag, 'error': 'device inventory limit exceeded'})
            return
        raw_devices = [d.replace('/dev/disk', '/dev/rdisk', 1) for d in devices]
        targets = [str(workspace.image), *devices, *raw_devices]
        _, raw, _ = self.command(tag + '-holders',
                                ['/usr/sbin/lsof', '-nP', '+c', '0', '-Fpcfn', '--', *targets])
        for pid, command in list(holders(raw.decode(errors='replace')).items())[:4]:
            if command.startswith('diskimage'):
                # This observes stacks; it does not infer process ownership or
                # terminate holders. PID reuse can invalidate a sample's attribution.
                self.command(tag + '-sample', ['/usr/bin/sample', str(pid), '1', '1', '-file',
                             str(self.output / f'{tag}-sample-{pid}.txt')], timeout=8)
        for device in devices:
            self.command(tag + '-disk-info', [native_storage.DISKUTIL, 'info', '-plist', device])
        self.command(tag + '-apfs', [native_storage.DISKUTIL, 'apfs', 'list', '-plist'])
        self.command(tag + '-ioreg', ['/usr/sbin/ioreg', '-r', '-c', 'IOMedia', '-l', '-w', '0'])

    @contextmanager
    def log_stream(self):
        process = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'compact', '--level', 'debug',
                                    '--predicate', PREDICATE], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, close_fds=True)
        total = [0]

        def drain():
            with (self.output / 'system-stream.log').open('wb') as target:
                while raw := process.stdout.read(4096):
                    remaining = max(0, MAX_STREAM - total[0])
                    target.write(raw[:remaining])
                    total[0] += len(raw)

        thread = threading.Thread(target=drain)
        thread.start()
        try:
            yield
        finally:
            if process.poll() is None:
                process.terminate()  # Only our own waitable log reader.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            thread.join(timeout=5)
            process.stdout.close()
            if thread.is_alive():
                raise RuntimeError('log reader failed to stop')
            self.emit({'tag': 'system-stream', 'returncode': process.returncode,
                       'bytes': total[0], 'truncated': total[0] > MAX_STREAM})


class ObservedWorkspace(native_storage.Workspace):
    def __init__(self, recorder):
        super().__init__(64)
        self.recorder = recorder

    def _eject(self, device, *, force):
        try:
            return super()._eject(device, force=force)
        except native_storage.StorageError as error:
            if 'Resource busy' in str(error) and not self.recorder.first_busy:
                self.recorder.first_busy = True
                self.recorder.emit({'tag': 'first-busy', 'image': str(self.image), 'force': force})
                if self.recorder.verbose:
                    self.recorder.snapshot(self, 'first-busy')
            raise


def probe(recorder, cycles):
    failures = 0
    with recorder.log_stream(), mock.patch.object(native_storage, '_run', recorder.run):
        recorder.command('runtime', ['/usr/bin/sw_vers'])
        recorder.command('hdiutil-help', [native_storage.HDIUTIL, 'help', 'detach'])
        # Match the earlier failure's ordering: real recovery tests preceded
        # the first idle stress image. Capture their logs before starting churn.
        precondition, _, _ = recorder.command(
            'recovery-precondition', [sys.executable, 'tests/leakguard.py', 'discover',
                                     '-v', '-s', 'tests', '-p', 'test_native_recovery.py'], timeout=180)
        precondition_failed = bool(precondition.get('error') or precondition['returncode'])
        for cycle in range(cycles):
            workspace = ObservedWorkspace(recorder)
            workload = ('idle', 'metadata', 'fsync')[cycle % 3]
            event = {'tag': 'cycle', 'cycle': cycle, 'workload': workload,
                     'architecture': platform.machine(), 'verbose': recorder.verbose,
                     'turn_processes_launched': False}
            started = time.monotonic()
            try:
                with workspace:
                    if workload == 'metadata':
                        for index in range(192):
                            path = workspace.work / f'file-{index}'
                            path.write_bytes(b'x' * 4096)
                            path.rename(workspace.work / f'renamed-{index}')
                        for path in workspace.work.iterdir():
                            path.unlink()
                    elif workload == 'fsync':
                        with (workspace.work / 'synced').open('wb') as target:
                            target.write(b'x' * (8 * 1024 * 1024))
                            target.flush()
                            os.fsync(target.fileno())
            except Exception as error:
                failures += 1
                event['error'] = str(error)
                # No extra detach or force: preserve the failure's attachment
                # and observe whether it settles without another mutation.
                if workspace.root is not None:
                    for delay, tag in ((0, 'failed'), (5, 'after-5s'), (10, 'after-15s'), (15, 'after-30s')):
                        if delay:
                            time.sleep(delay)
                        recorder.snapshot(workspace, tag)
                break
            finally:
                event.update(root=str(workspace.root), ejects=workspace.cleanup_events,
                             creation=workspace.creation_events,
                             elapsed_seconds=round(time.monotonic() - started, 3))
                recorder.emit(event)
        recorder.command('system-history', ['/usr/bin/log', 'show', '--last', '15m', '--style',
                         'compact', '--info', '--debug', '--predicate', PREDICATE], timeout=15)
    recorder.emit({'tag': 'result', 'failures': failures, 'first_busy': recorder.first_busy,
                   'precondition_failed': precondition_failed})
    return bool(failures or precondition_failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=64, choices=range(1, 129))
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    if reason := seatbelt.unavailable():
        parser.error(reason)
    raise SystemExit(bool(probe(Recorder(args.output, verbose=args.verbose), args.cycles)))
