"""Experimental host cleanup pool; receipts are the durable queue.

No production backend is selected here. Admission and sweeps share a private
host lease. Unknown ownership is an admission failure, never deletion authority.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from . import native_recovery as recovery


class AdmissionRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    max_pending: int = 4
    max_pending_bytes: int = 2 * 1024 ** 3
    max_pending_age: int = 300
    free_headroom: int = 256 * 1024 ** 2
    sweep_limit: int = 1

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError('cleanup policy requires positive integer limits')
        if self.sweep_limit > recovery.MAX_WORKSPACES:
            raise ValueError('cleanup sweep limit exceeds inventory bound')


class Manager:
    def __init__(self, root: Path, *, policy: Policy | None = None, create=True):
        self.root = Path(root).absolute()
        if create:
            self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.resolve(strict=True) != self.root:
            raise recovery.RecoveryError('cleanup root must be canonical, without symlinks')
        recovery._private(self.root.lstat(), directory=True)
        self.volumes = self.root / 'volumes'
        if create:
            self.volumes.mkdir(mode=0o700, exist_ok=True)
        recovery._private(self.volumes.lstat(), directory=True)
        self.policy = policy or Policy()
        if policy is None:
            try:
                self.policy = Policy(**self._load().get('policy', asdict(self.policy)))
            except (TypeError, ValueError) as error:
                raise recovery.RecoveryError('invalid saved cleanup policy') from error
        if create:
            for path in (self.root, self.root.parent):
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def _load(self, lease=None):
        path = self.root / recovery.JOURNAL
        try:
            recovery._private(path.lstat())
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                recovery._private(os.fstat(stream.fileno()))
                raw = stream.read(recovery.MAX_RECORD + 1)
            value = json.loads(raw)
            if len(raw) > recovery.MAX_RECORD or value.get('kind') != 'native-cleanup-pool' or value.get('version') != 1:
                raise ValueError('foreign cleanup state')
            if (type(value.get('paused')) is not bool or type(value.get('cursor')) is not str or
                    type(value.get('notices')) is not list or len(value['notices']) > 8 or
                    any(type(n) is not dict or type(n.get('message')) is not str or
                        n.get('state') not in ('pending', 'uncertain', 'delivered') or
                        type(n.get('id')) is not str for n in value['notices']) or
                    type(value.get('requested_bytes', 0)) is not int or value.get('requested_bytes', 0) < 0):
                raise ValueError('invalid cleanup state')
            return value
        except FileNotFoundError:
            if any(self.volumes.iterdir()):
                raise recovery.RecoveryError('cleanup control state missing with existing workspaces; admission refused')
            return {'version': 1, 'kind': 'native-cleanup-pool', 'paused': False,
                    'cursor': '', 'notices': []}
        except (ValueError, TypeError, AttributeError) as error:
            raise recovery.RecoveryError('invalid cleanup control state; admission refused') from error

    def _inventory(self):
        items = []
        with os.scandir(self.volumes) as entries:
            paths = []
            for count, entry in enumerate(entries):
                if count >= recovery.MAX_WORKSPACES:
                    items.append({'status': 'unknown', 'reason': 'cleanup inventory limit exceeded'})
                    break
                paths.append(Path(entry.path))
        for path in sorted(paths):
            try:
                if not path.name.startswith('dk-volume-'):
                    raise recovery.RecoveryError('foreign entry in cleanup pool')
                with recovery.disposal_lease(path), recovery.Record(path) as receipt:
                    record = receipt.read()
                if record.get('eventual_cleanup') is not True:
                    raise recovery.RecoveryError('workspace did not opt into this cleanup pool')
                items.append({'root': str(path), 'status': 'owned', 'state': record['state'],
                              'bytes': record['size_mib'] * 1024 * 1024,
                              'pending_since': record.get('pending_since', record.get('created_at', time.time())),
                              'last_error': record.get('last_cleanup_error', '')})
            except BlockingIOError:
                items.append({'root': str(path), 'status': 'active'})
            except (OSError, recovery.RecoveryError) as error:
                items.append({'root': str(path), 'status': 'unknown', 'reason': str(error)})
        return items

    def _summary(self, items, requested=0):
        pending = [i for i in items if i.get('state') in ('drained', 'disposed')]
        unknown = [i for i in items if i['status'] == 'unknown']
        uncertain = [i for i in items if i['status'] == 'owned' and i not in pending]
        count, size = len(pending), sum(i['bytes'] for i in pending)
        oldest = max((max(0, time.time() - i['pending_since']) for i in pending), default=0)
        reason = ''
        if unknown:
            reason = 'unknown cleanup ownership: ' + unknown[0]['reason']
        elif uncertain:
            reason = 'process completion unverified: ' + uncertain[0]['root']
        elif count >= self.policy.max_pending:
            reason = 'pending image count limit reached'
        elif size >= self.policy.max_pending_bytes:
            reason = 'pending storage limit reached'
        elif oldest >= self.policy.max_pending_age:
            reason = 'oldest pending image exceeded cleanup deadline'
        elif size + requested > self.policy.max_pending_bytes or shutil.disk_usage(self.volumes).free < requested + self.policy.free_headroom:
            reason = 'insufficient reserved capacity or free host storage'
        return {'paused': bool(reason), 'reason': reason[:512], 'pending_images': count,
                'reserved_bytes': size, 'oldest_seconds': round(oldest, 1),
                'active': sum(i['status'] == 'active' for i in items),
                'last_error': next((i['last_error'] for i in reversed(pending) if i['last_error']), '')[-512:]}

    def _refresh(self, lease, state, summary):
        if summary['paused'] != state['paused']:
            if len(state['notices']) >= 8:
                raise AdmissionRefused('cleanup notification outbox is full')
            paused = summary['paused']
            state['notices'].append({'id': uuid.uuid4().hex, 'state': 'pending',
                                    'message': describe(summary)[:640] if paused else 'native turns resumed: cleanup admission recovered'})
        state.update(summary, policy=asdict(self.policy))
        lease.write(state)

    def prepare(self, workspace):
        """Serialize admission and receipt registration before any image command."""
        with recovery.Record(self.root, create=True) as lease:
            state = self._load(lease)
            state['requested_bytes'] = workspace.size_bytes
            summary = self._summary(self._inventory(), workspace.size_bytes)
            self._refresh(lease, state, summary)
            if summary['paused']:
                raise AdmissionRefused(describe(summary))
            if summary['active']:
                raise AdmissionRefused('native turn already active; concurrency limit is one')
            workspace.root = Path(tempfile.mkdtemp(prefix='dk-volume-', dir=self.volumes)).resolve()
            workspace.image, workspace.mount = workspace.root / 'turn.dmg', workspace.root / 'mount'
            descriptor = os.open(workspace.root / 'disposal.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                receipt = recovery.register(workspace)
            except BaseException:
                os.close(descriptor)
                raise
            workspace._disposal_lease = descriptor
            return receipt

    def sweep(self, *, notify=None):
        """One bounded, rotating retry pass. Never signals saved process IDs."""
        with recovery.Record(self.root, create=True) as lease:
            state = self._load(lease)
            items = self._inventory()
            roots = sorted(i['root'] for i in items if i['status'] == 'owned')
            ordered = [p for p in roots if p > state['cursor']] + [p for p in roots if p <= state['cursor']]
            results = []
            for root in ordered[:self.policy.sweep_limit]:
                # Persist cursor first so a dying cleanup pass cannot starve others.
                state['cursor'] = root
                lease.write(state)
                results.append(recovery.recover(Path(root), defer=True))
            summary = self._summary(self._inventory(), state.get('requested_bytes', 0))
            self._refresh(lease, state, summary)
            if notify is not None:
                for notice in state['notices']:
                    if notice['state'] != 'pending':
                        continue
                    # Claim before handing off: a crash leaves an uncertain delivery,
                    # visible in status, rather than blindly replaying a notice.
                    notice['state'] = 'uncertain'
                    lease.write(state)
                    try:
                        notify(notice['message'])
                    except Exception:
                        continue
                    notice['state'] = 'delivered'
                    lease.write(state)
                state['notices'] = [n for n in state['notices'] if n['state'] != 'delivered']
                lease.write(state)
            return {'summary': summary, 'results': results}

    def status(self):
        """Read-only doctor/explain view; never retries detach or writes control state."""
        state = self._load()
        summary = self._summary(self._inventory(), state.get('requested_bytes', 0))
        return {**summary, 'notices': state['notices']}


def describe(summary):
    state = 'paused' if summary['paused'] else 'available'
    return (f"native turns {state}: {summary['pending_images']} images awaiting cleanup, "
            f"{summary['reserved_bytes']} bytes reserved, oldest {summary['oldest_seconds'] / 60:.1f} min"
            + (f"; {summary['reason']}" if summary['reason'] else '')
            + (f"; last error: {summary['last_error']}" if summary['last_error'] else ''))


def ledger_status(db):
    """Optional read-only install view. Absence means this experiment is unused."""
    root = Path(db).parent / 'native-cleanup'
    if not root.exists() and not root.is_symlink():
        return None
    try:
        summary = Manager(root, create=False).status()
        return {'paused': summary['paused'], 'detail': describe(summary),
                'uncertain_notices': sum(n['state'] == 'uncertain' for n in summary['notices'])}
    except (OSError, RuntimeError) as error:
        return {'paused': True, 'detail': 'native cleanup state unverified: ' + str(error),
                'uncertain_notices': 0}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--sweep', action='store_true')
    args = parser.parse_args()
    manager = Manager(args.root, create=False)
    result = manager.sweep(notify=print) if args.sweep else {'summary': manager.status()}
    print(json.dumps(result, sort_keys=True))
