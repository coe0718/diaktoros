"""Host-only ownership receipts for experimental native turn recovery.

An unlocked lease is not evidence that a turn exited. Only a durable drained
receipt permits same-boot storage disposal; recorded PIDs are diagnostics only.
The trusted host owns the private root and its parent. Seats must never receive
write access to either, the journal, lease, or backing image.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

JOURNAL = 'ownership.json'
LEASE = 'ownership.lock'
MAX_RECORD = 8192


class RecoveryError(RuntimeError):
    pass


def _identity(info):
    return [info.st_dev, info.st_ino]


def _private(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RecoveryError('ownership path is not private host storage')
    if not directory and info.st_nlink != 1:
        raise RecoveryError('ownership file has multiple links')


class Record:
    """A pinned private directory and exclusive lease, never a PID authority."""
    def __init__(self, root: Path, *, create=False):
        self.root = Path(root)
        self.directory = self.lease = None
        try:
            if not self.root.is_absolute() or self.root.resolve(strict=True) != self.root:
                raise RecoveryError('ownership root must be canonical and not a symlink')
            self.directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _private(os.fstat(self.directory), directory=True)
            self.lease = os.open(LEASE, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0),
                                 0o600, dir_fd=self.directory)
            _private(os.fstat(self.lease))
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise

    def close(self):
        for name in ('lease', 'directory'):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self):
        descriptor = os.open(JOURNAL, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
        with os.fdopen(descriptor, 'rb') as source:
            _private(os.fstat(source.fileno()))
            raw = source.read(MAX_RECORD + 1)
        try:
            record = json.loads(raw)
            if (len(raw) > MAX_RECORD or type(record['version']) is not int or record['version'] != 1 or
                    record['root'] != str(self.root) or
                    record['root_identity'] != _identity(os.fstat(self.directory)) or
                    type(record['size_mib']) is not int or
                    not 64 <= record['size_mib'] <= 16384 or
                    record['state'] not in ('armed', 'running', 'drained')):
                raise ValueError('invalid ownership record')
        except (ValueError, KeyError, TypeError) as error:
            raise RecoveryError('invalid ownership record; retain storage') from error
        image = os.stat('turn.dmg', dir_fd=self.directory, follow_symlinks=False)
        _private(image)
        if record.get('image_identity') != _identity(image):
            raise RecoveryError('backing image identity changed; retain storage')
        mount = os.stat('mount', dir_fd=self.directory, follow_symlinks=False)
        if not stat.S_ISDIR(mount.st_mode):
            raise RecoveryError('mount path is not a directory')
        if _identity(self.root.lstat()) != record['root_identity']:
            raise RecoveryError('ownership root was replaced')
        return record

    def write(self, record):
        raw = json.dumps(record, sort_keys=True).encode()
        if len(raw) > MAX_RECORD:
            raise RecoveryError('ownership receipt exceeds limit')
        name = '.ownership-' + uuid.uuid4().hex
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.directory)
        try:
            with os.fdopen(descriptor, 'wb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, JOURNAL, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(name, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def update(self, state, **details):
        record = self.read()
        allowed = {'armed': ('running', 'drained'), 'running': ('drained',), 'drained': ()}
        if state not in allowed[record['state']]:
            raise RecoveryError('invalid ownership transition')
        record.update(details, state=state)
        self.write(record)

    def cleanup(self):
        record = self.read()
        if record['state'] != 'drained':
            raise RecoveryError('turn cleanup is unverified; retain storage')
        from . import native_storage
        workspace = native_storage.Workspace(record['size_mib'])
        workspace.root = self.root
        workspace.image = self.root / 'turn.dmg'
        workspace.mount = self.root / 'mount'
        workspace._detach_owned()
        workspace._verify_detached()
        self.read()  # Revalidate the pinned image/root before removing anything.
        shutil.rmtree(self.root)


def arm(workspace):
    """Persist uncertainty before any guarded process can launch."""
    with Record(workspace.root, create=True) as receipt:
        try:
            os.stat(JOURNAL, dir_fd=receipt.directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RecoveryError('workspace already has an ownership record')
        image = workspace.image.lstat()
        _private(image)
        receipt.write({'version': 1, 'root': str(workspace.root),
                       'root_identity': _identity(os.fstat(receipt.directory)),
                       'image_identity': _identity(image), 'size_mib': workspace.size_mib,
                       'turn_id': uuid.uuid4().hex, 'state': 'armed'})


def recover(root: Path):
    """Reconcile one explicit owned root; never signal recorded processes."""
    try:
        with Record(root) as receipt:
            receipt.cleanup()
        return {'root': str(root), 'status': 'removed'}
    except BlockingIOError:
        return {'root': str(root), 'status': 'active'}
    except (RecoveryError, OSError, RuntimeError) as error:
        return {'root': str(root), 'status': 'retained', 'reason': str(error)}


def reconcile(roots):
    """Bounded caller-selected restart inventory; unknown roots remain untouched."""
    return [recover(Path(root)) for root in roots]


if __name__ == '__main__':
    import sys
    results = reconcile(sys.argv[1:])
    print(json.dumps(results, sort_keys=True))
    raise SystemExit(1 if any(result['status'] == 'retained' for result in results) else 0)
