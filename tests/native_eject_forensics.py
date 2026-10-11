"""Bounded, opt-in Mac eject forensics; never a production cleanup policy."""
import _home_guard  # noqa: F401
import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys
import threading
import time
from unittest import mock

from diaktoros import native_storage, seatbelt

# Routine Disk Arbitration callbacks exhausted the original stream before a
# captured failure. Keep lifecycle/rejection messages and driver diagnostics.
LIFECYCLE = ('eventMessage CONTAINS[c] "eject" OR eventMessage CONTAINS[c] "unmount" '
             'OR eventMessage CONTAINS[c] "busy" OR eventMessage CONTAINS[c] "dissent" '
             'OR eventMessage CONTAINS[c] "unable" OR eventMessage CONTAINS[c] "open failed"')
PREDICATE = ('process BEGINSWITH "diskimage" OR ((process == "diskarbitrationd" '
             'OR process == "fseventsd" OR process BEGINSWITH "mdworker" '
             'OR process == "mds" OR subsystem CONTAINS[c] "apfs") AND (' + LIFECYCLE + '))')
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


def process_rows(raw):
    """Parse BSD ps cumulative CPU time; identity fields are diagnostic only."""
    result = {}
    for line in raw.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9 or not fields[0].isdigit():
            continue
        cpu = re.fullmatch(r'(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)', fields[6])
        if cpu is None:
            continue
        days, hours, minutes, seconds = cpu.groups()
        result[int(fields[0])] = {
            'started': ' '.join(fields[1:6]), 'state': fields[7], 'command': fields[8],
            'cpu_seconds': int(days or 0) * 86400 + int(hours or 0) * 3600
                           + int(minutes) * 60 + float(seconds)}
    return result


def registry_connections(raw, devices):
    """Retain registry-visible client/provider paths, not a complete open census."""
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('registry archive exceeds analysis limit')
    roots = plistlib.loads(raw)
    if isinstance(roots, dict) and ('IORegistryEntryName' in roots or 'IOObjectClass' in roots):
        roots = [roots]
    if not isinstance(roots, list):
        raise ValueError('registry archive root is not an array')
    fields = ('IORegistryEntryID', 'IORegistryEntryName', 'IOObjectClass',
              'BSD Name', 'Open', 'IOUserClientCreator')
    media, clients, count = [], [], 0
    pending = [(root, []) for root in roots]
    while pending:
        node, ancestors = pending.pop()
        if not isinstance(node, dict):
            raise ValueError('invalid registry node')
        count += 1
        if count > 50000 or len(ancestors) > 128:
            raise ValueError('registry inventory limit exceeded')
        summary = {key: node[key] for key in fields if key in node}
        path = [*ancestors, summary]
        if '/dev/' + str(node.get('BSD Name', '')) in devices:
            media.append({'path': path})
        if 'IOUserClientCreator' in node or 'UserClient' in str(node.get('IOObjectClass', '')):
            clients.append({'path': path})
        pending.extend((child, path) for child in node.get('IORegistryEntryChildren', []))
    return {'media': media, 'user_clients': clients, 'nodes_examined': count,
            'scope': 'registry-visible objects only; absence does not exclude unregistered connections'}


class Recorder:
    def __init__(self, output, *, verbose=False, sync_delay=False, pre_unmount=False):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.verbose = verbose
        self.sync_delay = sync_delay or pre_unmount
        self.pre_unmount = pre_unmount
        self.sequence = 0
        self.first_busy = False
        self.helper_baselines = {}
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

    def before_detach(self, workspace):
        if not self.sync_delay:
            return
        # macOS sync is system-wide, not a volume-specific flush.
        flush, _, _ = self.command('pre-detach-sync', ['/bin/sync'], timeout=15)
        if flush.get('error') or flush['returncode']:
            raise RuntimeError('pre-detach sync failed')
        time.sleep(2)
        self.emit({'tag': 'pre-detach-wait', 'seconds': 2, 'image': str(workspace.image)})
        if self.pre_unmount:
            for device in workspace._devices():
                # Resolve exact-image ownership again immediately before mutation.
                if device not in workspace._devices():
                    continue
                outcome, _, _ = self.command('pre-detach-unmount',
                                             [native_storage.DISKUTIL, 'unmountDisk', 'force', device],
                                             timeout=30)
                if outcome.get('error') or outcome['returncode']:
                    raise RuntimeError('experimental pre-detach unmount failed')

    def helper_progress(self, workspace, phase):
        image = str(workspace.image)
        baseline = self.helper_baselines.get(image, {})
        # Include baseline PIDs when the image has detached or a helper vanished.
        # These IDs are queried only, never used as signal/ownership authority.
        _, raw, _ = self.command(phase + '-helper-holders',
                                ['/usr/sbin/lsof', '-nP', '+c', '0', '-Fpcfn', '--', image])
        current = {pid for pid, name in holders(raw.decode(errors='replace')).items()
                   if name.startswith('diskimage')}
        pids = sorted(current | set(baseline))[:4]
        rows = {}
        if pids:
            _, raw, _ = self.command(phase + '-helper-cpu',
                                    ['/bin/ps', '-p', ','.join(map(str, pids)), '-o', 'pid=',
                                     '-o', 'lstart=', '-o', 'time=', '-o', 'state=', '-o', 'comm='])
            rows = process_rows(raw.decode(errors='replace'))
        observations = []
        for pid in pids:
            row, previous = rows.get(pid), baseline.get(pid)
            same = bool(row and previous and row['started'] == previous['started']
                        and row['command'] == previous['command'])
            delta = round(row['cpu_seconds'] - previous['cpu_seconds'], 3) if same else None
            observations.append({'pid': pid, 'holds_image': pid in current, 'process': row,
                                 'identity_fields_match': same,
                                 'cpu_delta_seconds': delta if delta is not None and delta >= 0 else None})
        if phase == 'first-failure':
            self.helper_baselines[image] = rows
        self.emit({'tag': 'helper-progress', 'phase': phase, 'image': image,
                   'observations': observations,
                   'identity_precision': 'ps lstart seconds; PID reuse remains possible',
                   'cpu_precision': 'ps display precision; zero delta does not prove a hang'})

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
        # Observe media opens before diskutil queries or helper sampling can
        # change the timing. Compare with the second registry snapshot below.
        self.command(tag + '-ioreg-before', ['/usr/sbin/ioreg', '-r', '-c', 'IOMedia', '-l', '-w', '0'])
        event, raw, _ = self.command(tag + '-registry-connections',
                                    ['/usr/sbin/ioreg', '-a', '-l', '-p', 'IOService'], timeout=8)
        try:
            if event.get('error') or event.get('returncode') != 0:
                raise ValueError('registry archive unavailable')
            evidence = registry_connections(raw, devices)
            encoded = json.dumps(evidence, default=str).encode()
            if len(encoded) > MAX_OUTPUT:
                raise ValueError('registry relationship output exceeds limit')
            (self.output / f'{tag}-registry-connections.json').write_bytes(encoded)
        except Exception as error:
            self.emit({'tag': tag + '-registry-analysis', 'error': str(error)})
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
        # A short retrospective query reaches the actual failure even if the
        # live stream has filled. Standard mode calls this only after exhaustion.
        self.command(tag + '-history', ['/usr/bin/log', 'show', '--last', '2m', '--style',
                     'compact', '--info', '--debug', '--predicate', PREDICATE], timeout=15)

    @contextmanager
    def log_stream(self):
        process = subprocess.Popen(['/usr/bin/log', 'stream', '--style', 'compact', '--level', 'debug',
                                    '--predicate', PREDICATE], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, close_fds=True)
        total = [0]
        tail = deque()
        tail_size = 0

        def drain():
            nonlocal tail_size
            with (self.output / 'system-stream.log').open('wb') as target:
                while raw := process.stdout.read(4096):
                    remaining = max(0, MAX_STREAM - total[0])
                    target.write(raw[:remaining])
                    total[0] += len(raw)
                    tail.append(raw)
                    tail_size += len(raw)
                    while tail_size > MAX_STREAM:
                        excess = tail_size - MAX_STREAM
                        first = tail.popleft()
                        removed = min(excess, len(first))
                        if removed < len(first):
                            tail.appendleft(first[removed:])
                        tail_size -= removed
            (self.output / 'system-stream-tail.log').write_bytes(b''.join(tail))

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
                       'bytes': total[0], 'truncated': total[0] > MAX_STREAM,
                       'tail_bytes': tail_size})


class ObservedWorkspace(native_storage.Workspace):
    def __init__(self, recorder):
        super().__init__(64)
        self.recorder = recorder
        self.had_busy = False

    def _eject(self, device, *, force):
        try:
            return super()._eject(device, force=force)
        except native_storage.StorageError as error:
            if 'Resource busy' in str(error) and not self.had_busy:
                self.had_busy = True
                self.recorder.helper_progress(self, 'first-failure')
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
                     'sync_delay': recorder.sync_delay,
                     'pre_unmount': recorder.pre_unmount,
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
                    recorder.before_detach(workspace)
                if workspace.had_busy:
                    recorder.helper_progress(workspace, 'cleanup-success')
            except Exception as error:
                failures += 1
                event['error'] = str(error)
                if workspace.had_busy:
                    recorder.helper_progress(workspace, 'give-up')
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
    parser.add_argument('--sync-delay', action='store_true',
                        help='test-only system-wide sync plus two seconds before first detach')
    parser.add_argument('--sync-unmount', action='store_true',
                        help='sync-delay plus forced unmount of freshly verified owned whole disks')
    args = parser.parse_args()
    if reason := seatbelt.unavailable():
        parser.error(reason)
    raise SystemExit(bool(probe(Recorder(args.output, verbose=args.verbose,
                                       sync_delay=args.sync_delay,
                                       pre_unmount=args.sync_unmount), args.cycles)))
