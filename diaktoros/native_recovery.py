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
import subprocess
import sys
import uuid

JOURNAL = 'ownership.json'
LEASE = 'ownership.lock'
MAX_RECORD = 8192
MAX_INVENTORY = 10000
MAX_WORKSPACES = 128
PREPARATION_STATES = ('registered', 'created', 'attached')


class RecoveryError(RuntimeError):
    pass


def _valid_boot_id(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def boot_identity():
    """Read a kernel boot session, never infer reboot from elapsed time or PIDs."""
    if sys.platform != 'darwin':
        return None
    try:
        result = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=5, check=True, env={})
        value = result.stdout.strip().lower()
        if not _valid_boot_id(value):
            raise ValueError('invalid kernel boot session UUID')
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RecoveryError('kernel boot identity unavailable; retain storage') from error


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
            if not create:
                _private(os.stat(LEASE, dir_fd=self.directory, follow_symlinks=False))
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
        _private(os.stat(JOURNAL, dir_fd=self.directory, follow_symlinks=False))
        descriptor = os.open(JOURNAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self.directory)
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
                    record['state'] not in (*PREPARATION_STATES, 'armed', 'running', 'drained', 'disposed')):
                raise ValueError('invalid ownership record')
            if 'boot_id' in record and record['boot_id'] is not None and not _valid_boot_id(record['boot_id']):
                raise ValueError('invalid recorded boot identity')
            if record['state'] in PREPARATION_STATES and (
                    record.get('image_path') != str(self.root / 'turn.dmg') or
                    record.get('owner_uid') != os.getuid() or
                    record.get('reserved_bytes') != record['size_mib'] * 1024 * 1024):
                raise ValueError('invalid preparation ownership')
        except (ValueError, KeyError, TypeError) as error:
            raise RecoveryError('invalid ownership record; retain storage') from error
        try:
            image = os.stat('turn.dmg', dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            if record['state'] not in ('registered', 'disposed'):
                raise
        else:
            # Creation may leave a partial image before chmod/identity binding.
            # Such a receipt is discovery evidence, never disposal authority.
            if record['state'] == 'registered':
                if not stat.S_ISREG(image.st_mode) or image.st_uid != os.getuid() or image.st_nlink != 1:
                    raise RecoveryError('invalid partial image; retain storage')
            else:
                _private(image)
            if record['state'] != 'registered' and record.get('image_identity') != _identity(image):
                raise RecoveryError('backing image identity changed; retain storage')
        try:
            mount = os.stat('mount', dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            if record['state'] not in ('registered', 'disposed'):
                raise
        else:
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
        allowed = {'registered': ('created',), 'created': ('attached',), 'attached': ('armed',),
                   'armed': ('running', 'drained'), 'running': ('drained',),
                   'drained': ('disposed',), 'disposed': ()}
        if state not in allowed[record['state']]:
            raise RecoveryError('invalid ownership transition')
        record.update(details, state=state)
        self.write(record)

    def cleanup(self):
        record = self.read()
        if record['state'] in ('created', 'attached', 'armed', 'running') and record.get('boot_id'):
            current_boot = boot_identity()
            if current_boot is not None and current_boot != record['boot_id']:
                # A changed kernel boot proves old execution ended. It does not
                # authorize detaching an image reattached in this boot. Verify
                # absence first, then persist disposal proof before deletion.
                from . import native_storage
                workspace = native_storage.Workspace(record['size_mib'])
                workspace.root, workspace.image = self.root, self.root / 'turn.dmg'
                workspace.mount = self.root / 'mount'
                if workspace._devices():
                    raise RecoveryError('image attached after boot change; retain storage')
                if workspace.mount.stat().st_dev != self.root.stat().st_dev:
                    raise RecoveryError('mount remains mounted after boot change; retain storage')
                record.update(state='disposed', recovery_boot_id=current_boot,
                              recovery_reason='verified kernel boot change')
                self.write(record)
        if record['state'] in PREPARATION_STATES:
            raise RecoveryError('workspace preparation interrupted at ' + record['state'] +
                                '; creator completion is unverified; retain storage')
        if record['state'] not in ('drained', 'disposed'):
            raise RecoveryError('turn cleanup is unverified; retain storage')
        from . import native_storage
        workspace = native_storage.Workspace(record['size_mib'])
        workspace.root = self.root
        workspace.image = self.root / 'turn.dmg'
        workspace.mount = self.root / 'mount'
        if record['state'] == 'drained':
            workspace._detach_owned()
            workspace._verify_detached()
            self.update('disposed')
        else:
            # A restart can find the image/mount removed after verified disposal.
            # Recheck exact-image attachments; never interpret absence alone as
            # process cleanup proof. The disposed receipt supplies that proof.
            if workspace._devices():
                raise RecoveryError('disposed image was reattached; retain storage')
            if workspace.mount.exists() and workspace.mount.stat().st_dev != self.root.stat().st_dev:
                raise RecoveryError('disposed mount is still mounted; retain storage')
        self.read()  # Revalidate the pinned image/root before removing anything.
        # Preserve the lease and receipt until all other owned contents are gone.
        # A crash halfway through traversal remains recoverable as disposed.
        for path in self.root.iterdir():
            if path.name in (JOURNAL, LEASE):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        os.unlink(JOURNAL, dir_fd=self.directory)
        os.unlink(LEASE, dir_fd=self.directory)
        self.root.rmdir()


def register(workspace):
    """Write ownership before hdiutil runs; caller holds returned preparation lease.

    The parent directory entry is fsynced before image creation is permitted.
    An abandoned preparation receipt does not prove hdiutil or a turn exited.
    """
    receipt = Record(workspace.root, create=True)
    try:
        if (workspace.root / JOURNAL).exists() or (workspace.root / JOURNAL).is_symlink():
            raise RecoveryError('workspace already has an ownership record')
        if workspace.image != workspace.root / 'turn.dmg' or workspace.image.exists() or workspace.image.is_symlink():
            raise RecoveryError('registration requires an absent exact image path')
        receipt.write({'version': 1, 'root': str(workspace.root),
                       'root_identity': _identity(os.fstat(receipt.directory)),
                       'image_path': str(workspace.image), 'owner_uid': os.getuid(),
                       'reserved_bytes': workspace.size_mib * 1024 * 1024,
                       'size_mib': workspace.size_mib, 'turn_id': uuid.uuid4().hex,
                       'state': 'registered', 'boot_id': boot_identity()})
        parent = os.open(workspace.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return receipt
    except BaseException:
        receipt.close()
        raise


def arm(workspace):
    """Persist uncertainty before any guarded process can launch."""
    preparation = getattr(workspace, '_preparation', None)
    if preparation is not None:
        try:
            preparation.update('armed')
        finally:
            preparation.close()
            workspace._preparation = None
        return
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
                       'turn_id': uuid.uuid4().hex, 'state': 'armed', 'boot_id': boot_identity()})


def recover(root: Path):
    """Reconcile one explicit owned root; never signal recorded processes."""
    try:
        with Record(root) as receipt:
            receipt.cleanup()
        return {'root': str(root), 'status': 'removed'}
    except BlockingIOError:
        return {'root': str(root), 'status': 'active'}
    except FileNotFoundError as error:
        return {'root': str(root), 'status': ('absent' if not Path(root).exists() and
                                            not Path(root).is_symlink() else 'retained'),
                'reason': str(error)}
    except (RecoveryError, OSError, RuntimeError) as error:
        return {'root': str(root), 'status': 'retained', 'reason': str(error)}


def reconcile(roots):
    """Bounded caller-selected restart inventory; unknown roots remain untouched."""
    return [recover(Path(root)) for root in roots]


def startup(parent: Path | None = None):
    """Discover a bounded host inventory; a directory name grants no authority."""
    parent = Path('/tmp') if parent is None else Path(parent)
    results, roots = [], []
    try:
        parent = parent.resolve(strict=True)
        with os.scandir(parent) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_INVENTORY or len(roots) >= MAX_WORKSPACES:
                    results.append({'root': str(parent), 'status': 'retained',
                                    'reason': 'native workspace inventory limit exceeded'})
                    break
                if entry.name.startswith('dk-volume-'):
                    roots.append(Path(entry.path))
        results.extend(reconcile(sorted(roots)))
    except OSError as error:
        results.append({'root': str(parent), 'status': 'retained', 'reason': str(error)})
    return results


if __name__ == '__main__':
    import sys
    results = startup() if sys.argv[1:] == ['--discover'] else reconcile(sys.argv[1:])
    print(json.dumps(results, sort_keys=True))
    raise SystemExit(1 if any(result['status'] == 'retained' for result in results) else 0)
