"""Experimental fixed-capacity APFS workspace; no parent-death/lifecycle guarantee.

The host owns the image and mount. Seats only receive directories inside it;
neither the backing image nor disk-device access belongs in their Seatbelt roots.
The limit bounds allocated filesystem storage, not sparse-file logical lengths.
"""
from __future__ import annotations

from pathlib import Path
import os
import json
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

HDIUTIL = '/usr/bin/hdiutil'
DISKUTIL = '/usr/sbin/diskutil'
EJECT_BACKOFF = (2, 4, 6, 8)  # Five normal attempts, twenty seconds of settling time.


class StorageError(RuntimeError):
    pass


def _diagnostic(tag, event):
    # The independent guardian can outlive the supervisor that read its pipe.
    # Diagnostics must not abort verified disposal when that reader disappears.
    # Write directly so a failed buffered flush cannot also break helper exit.
    raw = (tag + ' ' + json.dumps(event, default=str) + '\n').encode()
    try:
        descriptor = sys.stderr.fileno()
        while raw:
            written = os.write(descriptor, raw)
            if written <= 0:
                break
            raw = raw[written:]
    except (OSError, ValueError):
        pass


def _run(argv: list[str]) -> bytes:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, check=False)
    if result.returncode:
        raise StorageError(f'{Path(argv[0]).name} failed: ' +
                           (result.stdout + result.stderr).decode(errors='replace')[-2000:])
    return result.stdout


class Workspace:
    """Host-created disposable volume, with a shared allocation budget for all writes.

    Paths are usable only while entered. Cleanup never deletes a backing image
    until hdiutil confirms it is detached. Failed cleanup retains the private
    directory for recovery; it must not be interpreted as completed lifecycle
    containment or reliable cleanup after supervisor death.
    """
    def __init__(self, size_mib: int = 512, *, durable_ownership: bool = False):
        if type(size_mib) is not int or not 64 <= size_mib <= 16384:
            raise ValueError('workspace size must be 64..16384 MiB')
        self.size_bytes = size_mib * 1024 * 1024
        self.size_mib = size_mib
        self.root: Path | None = None
        self.active = False
        self.retained_reason = ''
        self.cleanup_events = []
        self.creation_events = []
        self.durable_ownership = durable_ownership
        self._preparation = None

    def retain(self, reason: str):
        """Preserve an owned image when its execution owner cannot confirm cleanup."""
        self.active = False
        self.retained_reason = reason or 'execution cleanup could not be verified'

    def __enter__(self):
        if self.root is not None:
            raise StorageError('workspace objects cannot be reused')
        if sys.platform != 'darwin' or not all(Path(p).is_file() for p in (HDIUTIL, DISKUTIL)):
            raise StorageError('native bounded storage requires macOS disk-image tools')
        self.root = Path(tempfile.mkdtemp(prefix='dk-volume-', dir='/tmp')).resolve()
        self.image = self.root / 'turn.dmg'
        self.mount = self.root / 'mount'
        try:
            if self.durable_ownership:
                from . import native_recovery
                self._preparation = native_recovery.register(self)
            self.mount.mkdir(mode=0o700)
            self._create_image()
            self.image.chmod(0o600)
            if self._preparation is not None:
                self._preparation.update('created', image_identity=native_recovery._identity(self.image.lstat()))
            image_info = plistlib.loads(_run([HDIUTIL, 'imageinfo', '-plist', str(self.image)]))
            if image_info.get('Format') != 'UDRW':
                raise StorageError('blank image is not fixed-size UDRW')
            _run([HDIUTIL, 'attach', '-plist', '-nobrowse', '-noautoopen',
                  '-owners', 'on', '-mountpoint', str(self.mount), str(self.image)])
            self._verify_mount()
            if self._preparation is not None:
                self._preparation.update('attached')
            # This is disposable turn storage, not an indexed or event-backed
            # user volume. Install opt-out markers before creating turn files.
            (self.mount / '.metadata_never_index').touch(mode=0o600)
            event_log = self.mount / '.fseventsd'
            event_log.mkdir(mode=0o700, exist_ok=True)
            (event_log / 'no_log').touch(mode=0o600)
            # Git trees require distinct case-sensitive names, even on a default Mac host.
            case_paths = (self.mount / 'CaseProbe', self.mount / 'caseprobe')
            for path in case_paths:
                with path.open('x'):
                    pass
            for path in case_paths:
                path.unlink()
            self.home, self.work, self.scratch = (self.mount / n for n in ('home', 'work', 'scratch'))
            for path in (self.home, self.work, self.scratch):
                path.mkdir(mode=0o700)
            self.active = True
            return self
        except BaseException:
            self._cleanup()
            raise

    def _attachments(self) -> list[dict]:
        info = plistlib.loads(_run([HDIUTIL, 'info', '-plist']))
        return [image for image in info.get('images', [])
                if Path(image.get('image-path', '')).resolve() == self.image]

    def _create_image(self):
        # Creating/formatting APFS itself uses an attached image. Its internal
        # eject can report busy before any turn has launched. Never accept that
        # partial result or overwrite an image that remains attached.
        for attempt in range(3):
            event = {'stage': 'create', 'attempt': attempt + 1, 'image': str(self.image)}
            started = time.monotonic()
            try:
                try:
                    _run([HDIUTIL, 'create', '-size', f'{self.size_mib}m', '-fs', 'APFS',
                          '-fsargs', '-e', '-type', 'UDIF', '-layout', 'GPTSPUD',
                          '-volname', 'Diaktoros', str(self.image)])
                except BaseException as error:
                    event['error'] = str(error)[-2000:]
                    raise
                finally:
                    event['elapsed_seconds'] = round(time.monotonic() - started, 3)
                    if 'error' in event:
                        try:
                            event['attachments'] = [
                                {'image': str(self.image), 'entities': [
                                    {k: entity[k] for k in ('dev-entry', 'mount-point') if k in entity}
                                    for entity in attachment.get('system-entities', [])]}
                                for attachment in self._attachments()]
                        except Exception as error:
                            event['diagnostic_error'] = str(error)[-2000:]
                    self.creation_events.append(event)
                    _diagnostic('NATIVE_STORAGE', event)
            except StorageError as error:
                if 'Resource busy' not in str(error) or attempt == 2:
                    raise
                self._detach_owned()
                self._verify_detached()
                self.image.unlink(missing_ok=True)
                time.sleep(2 * (attempt + 1))
            else:
                return

    def _devices(self) -> list[str]:
        """Discover current attachments by this exact private image, never a cached disk id."""
        devices = []
        for image in self._attachments():
            entities = image.get('system-entities', [])
            whole = [e.get('dev-entry', '') for e in entities
                     if re.fullmatch(r'/dev/disk[0-9]+', e.get('dev-entry', ''))]
            if not whole:
                raise StorageError('owned image has no identifiable whole disk; retained for recovery')
            devices.append(whole[0])
        return devices

    def _verify_mount(self):
        info = plistlib.loads(_run([DISKUTIL, 'info', '-plist', str(self.mount)]))
        attachments = self._attachments()
        owned_mount = any(e.get('dev-entry') == info.get('DeviceNode') and
                          e.get('mount-point') == str(self.mount)
                          for image in attachments for e in image.get('system-entities', []))
        if (info.get('FilesystemType', '').lower() != 'apfs' or
                Path(info.get('MountPoint', '')).resolve() != self.mount or
                self.mount.stat().st_dev == self.root.stat().st_dev or
                len(attachments) != 1 or not owned_mount):
            raise StorageError('workspace is not the expected attached APFS filesystem')
        stats = os.statvfs(self.mount)
        capacity = stats.f_blocks * stats.f_frsize
        if not 0 < capacity <= self.size_bytes:
            raise StorageError('filesystem capacity exceeds the configured bound')
        if self.image.stat().st_size > self.size_bytes + 1024 * 1024:
            raise StorageError('backing image exceeds fixed-size format overhead allowance')

    def validate(self, *, home: Path, work: Path, scratch: Path):
        if not self.active:
            raise StorageError('workspace must be mounted and active before launch')
        self._verify_mount()
        expected = (self.home, self.work, self.scratch)
        supplied = tuple(Path(p).resolve(strict=True) for p in (home, work, scratch))
        if supplied != expected or any(p.stat().st_dev != self.mount.stat().st_dev for p in supplied):
            raise StorageError('all writable turn roots must belong to the bounded workspace')

    def _cleanup(self):
        self.active = False
        if self._preparation is not None:
            self._preparation.close()
            self._preparation = None
        if self.root is None:
            return
        if self.retained_reason:
            raise StorageError(f'workspace retained at {self.root}: {self.retained_reason}')
        if (self.root / 'ownership.json').exists():
            from . import native_recovery
            result = native_recovery.recover(self.root)
            if result['status'] != 'removed':
                raise StorageError(f'workspace retained at {self.root}: {result}')
            return
        try:
            self._detach_owned()
            self._verify_detached()
        except BaseException as exc:
            raise StorageError(f'workspace cleanup failed; retained at {self.root}') from exc
        shutil.rmtree(self.root)

    def _verify_detached(self):
        if self._devices() or self.mount.stat().st_dev != self.root.stat().st_dev:
            raise StorageError('workspace remains attached')

    def _detach_owned(self):
        """Allow busy image machinery to settle before a single final forced attempt.

        A failed eject may already have unmounted the filesystem. Never reuse its
        disk id without checking the exact image again, including before force.
        Retries do not establish that detached seat descendants have exited.
        """
        for attempt in range(len(EJECT_BACKOFF) + 1):
            try:
                for device in self._devices():
                    if device not in self._devices():
                        continue
                    self._eject(device, force=False)
                if not self._devices():
                    return
                raise StorageError('workspace remains attached')
            except StorageError as exc:
                if 'Resource busy' not in str(exc):
                    raise
                # A failed command may nevertheless have completed detachment.
                # Do not wait or force a device that no longer belongs to us.
                if not self._devices():
                    return
                if attempt < len(EJECT_BACKOFF):
                    time.sleep(EJECT_BACKOFF[attempt])
                else:
                    for device in self._devices():
                        if device in self._devices():
                            try:
                                self._eject(device, force=True)
                            except StorageError as error:
                                # The final command can also report busy after
                                # completing detachment. Verify exact-image
                                # absence; never accept an attached image.
                                if 'Resource busy' not in str(error) or self._devices():
                                    raise
                    if self._devices():
                        raise StorageError('workspace remains attached after final eject')
                    return

    def _eject(self, device: str, *, force: bool):
        started = time.monotonic()
        event = {'device': device, 'force': force}
        try:
            return _run([HDIUTIL, 'detach', *(['-force'] if force else []), device])
        except BaseException as exc:
            event['error'] = str(exc)[-2000:]
            raise
        finally:
            event['elapsed_seconds'] = round(time.monotonic() - started, 3)
            # Gather evidence immediately: a later workflow step can observe a
            # different mount state after Disk Arbitration has continued cleanup.
            try:
                event['attachments'] = [
                    {'image': str(self.image), 'entities': [
                        {k: e[k] for k in ('dev-entry', 'mount-point') if k in e}
                        for e in attachment.get('system-entities', [])]}
                    for attachment in self._attachments()] if self.root is not None else []
                event['mounted'] = (self.mount.stat().st_dev != self.root.stat().st_dev
                                    if self.root is not None else None)
                if event.get('error') and event['mounted'] and any(
                        e.get('mount-point') == str(self.mount)
                        for attachment in event['attachments'] for e in attachment['entities']):
                    users = subprocess.run(['/usr/sbin/lsof', '-nP', '+f', '--', str(self.mount)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=5, check=False)
                    event['volume_users'] = (users.stdout + users.stderr).decode(
                        errors='replace')[-6000:]
                    event['volume_users_returncode'] = users.returncode
                    event['volume_users_stderr'] = users.stderr.decode(errors='replace')[-2000:]
                elif event.get('error') and event['attachments'] and not event['mounted']:
                    # Partial unmount removes the volume-holder query target.
                    # Observe the exact backing image and freshly rediscovered
                    # owned disk nodes; these opens do not by themselves prove
                    # the cause (disk-image machinery normally holds the image).
                    devices = sorted({e['dev-entry'] for attachment in event['attachments']
                                      for e in attachment['entities']
                                      if re.fullmatch(r'/dev/disk[0-9]+(?:s[0-9]+)*',
                                                      e.get('dev-entry', ''))})
                    if len(devices) > 32:
                        raise StorageError('owned-device diagnostic limit exceeded')
                    targets = [str(self.image), *devices]
                    event['backing_users_targets'] = targets
                    users = subprocess.run(['/usr/sbin/lsof', '-nP', '--', *targets],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           timeout=5, check=False)
                    event['backing_users'] = users.stdout.decode(errors='replace')[-6000:]
                    event['backing_users_returncode'] = users.returncode
                    event['backing_users_stderr'] = users.stderr.decode(errors='replace')[-2000:]
            except Exception as exc:
                event['diagnostic_error'] = str(exc)[-2000:]
            self.cleanup_events.append(event)
            _diagnostic('NATIVE_EJECT', event)

    def __exit__(self, *_):
        self._cleanup()
