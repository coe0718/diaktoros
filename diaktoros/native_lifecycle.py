"""Experimental independent watchdog for a native turn's original process group.

The host-only lifeline never reaches the sandboxed executable. EOF, timeout,
and normal leader exit all kill the original group before reaping its leader.
The restricted recovery mode keeps this helper as the cleanup guardian and
adds a disposable watchdog and a trusted group anchor. Watchdog/supervisor
death drains the reserved group. Guardian death makes the anchor kill its own
group; a surviving host needs live exit and empty-group proof before disposal.
Loss of both cleanup helpers remains fail-closed, not a descendant tracker.
"""
from __future__ import annotations

from contextlib import closing
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

MAX_CONFIG = 1024 * 1024
MAX_STATUS = 1024
MAX_GROUP_MEMBERS = 65536
GROUP_DRAIN_TIMEOUT = 5


class CleanupIncomplete(RuntimeError):
    """Watchdog completion cannot be verified; storage must be retained."""


class WatchdogLost(RuntimeError):
    """The guardian proved group cleanup and storage disposal after watchdog death."""


class GuardianLost(WatchdogLost):
    """Live anchor exit and empty-group proof authorized guardian-loss cleanup."""


class _AnchorProof:
    """Ephemeral kernel evidence; never reconstructed from journal PIDs."""
    def __init__(self, channel, guardian, turn_id):
        self.events = select.kqueue()
        self.pid = None
        try:
            channel.settimeout(5)
            raw = b''
            while not raw.endswith(b'\n') and len(raw) <= MAX_STATUS:
                value = channel.recv(1)
                if not value:
                    raise CleanupIncomplete('anchor handshake closed before registration')
                raw += value
            record = json.loads(raw)
            if (len(raw) > MAX_STATUS or type(record['pid']) is not int or record['pid'] <= 1 or
                    record['guardian'] != guardian.pid or record['turn_id'] != turn_id):
                raise CleanupIncomplete('invalid native anchor handshake')
            self.pid = record['pid']
            self.events.control([select.kevent(self.pid, filter=select.KQ_FILTER_PROC,
                                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                              fflags=select.KQ_NOTE_EXIT)], 0, 0)
            channel.sendall(b'x')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.events.close()

    def recover(self, root, turn_id):
        from . import native_recovery
        deadline = time.monotonic() + GROUP_DRAIN_TIMEOUT
        exited = False
        while True:
            # Registration preceded launch acknowledgement; NOTE_EXIT is live
            # evidence. Empty membership proves quiescence without signalling
            # a group whose leader reservation died with the guardian.
            ready = self.events.control(None, 1, min(0.025, max(0, deadline - time.monotonic())))
            exited = exited or any(e.filter == select.KQ_FILTER_PROC and e.ident == self.pid
                                   for e in ready)
            if exited and not _group_members(self.pid):
                break
            if time.monotonic() >= deadline:
                raise CleanupIncomplete('guardian lost; anchor group exit is unverified')
        with native_recovery.Record(root) as receipt:
            record = receipt.read()
            if record.get('turn_id') != turn_id:
                raise CleanupIncomplete('guardian-loss receipt belongs to another turn')
            if record['state'] in ('armed', 'running'):
                receipt.update('drained', cause='guardian_lost_anchor_exit')
            receipt.cleanup()
        raise GuardianLost('native guardian died; live anchor exit proved group cleanup and storage disposal')


def _sentinel(owner, timeout):
    """Disposable monitor; the guardian retains the waitable turn leader."""
    if os.getppid() != owner:
        return
    with closing(select.kqueue()) as events:
        try:
            events.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                          flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                          fflags=select.KQ_NOTE_EXIT)
                            for pid in (owner,)], 0, 0)
        except ProcessLookupError:
            return
        if os.getppid() == owner:
            events.control(None, 1, timeout + 1)


def _group_members(pid: int) -> set[int]:
    """Snapshot live and zombie members of the reserved original group.

    This is one group-membership check while the known leader's PID is reserved,
    not a descendant tracker or a source of PIDs to signal individually.
    proc_listpgrppids returns a PID count (not bytes); full buffers are retried.
    """
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    query = library.proc_listpgrppids
    query.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    capacity = 256
    while capacity <= MAX_GROUP_MEMBERS:
        members = (ctypes.c_int * capacity)()
        count = query(pid, members, ctypes.sizeof(members))
        if count < 0:
            raise OSError(ctypes.get_errno(), 'cannot verify native process group')
        if count < capacity:
            return set(members[:count])
        capacity *= 2
    raise CleanupIncomplete('native process group exceeds verification limit')


def _drain_group(pid: int, leader_exited: bool) -> None:
    """Kill only the reserved group and verify quiescence before reaping.

    Exit notification proves the leader stopped; libproc also includes zombies,
    so membership is rechecked until orphan reaping leaves only our waitable
    child. Neither signal delivery nor an exited-PID cache proves completion.
    """
    deadline = time.monotonic() + GROUP_DRAIN_TIMEOUT
    with closing(select.kqueue()) as events:
        if not leader_exited:
            try:
                events.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                              fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    raise CleanupIncomplete('cannot observe native leader exit') from error
                leader_exited = True
        while True:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Darwin can reject signalling zombie-only groups. An error
                # never proves cleanup: the exit and membership checks do.
                pass
            try:
                members = _group_members(pid)
            except OSError as error:
                raise CleanupIncomplete('cannot verify native group cleanup') from error
            if pid not in members:
                raise CleanupIncomplete('native leader reservation was lost')
            if leader_exited and members == {pid}:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CleanupIncomplete('native group cleanup deadline exceeded; '
                                        f'remaining members={len(members)}')
            # Group snapshots include zombies whose eventual reaping produces
            # no NOTE_EXIT. Bound this check; never signal discovered PIDs.
            ready = events.control(None, 1, min(0.025, remaining))
            leader_exited = leader_exited or any(
                event.filter == select.KQ_FILTER_PROC and event.ident == pid for event in ready)


def _watch(config_fd: int, lifeline_fd: int, status_fd: int):
    # Configuration comes from a trusted anonymous file, not child arguments or state.
    with os.fdopen(config_fd, 'rb') as source:
        config = json.loads(source.read(MAX_CONFIG + 1))
    argv, env, timeout, cwd, owner = (config[k] for k in ('argv', 'env', 'timeout', 'cwd', 'owner'))
    receipt = None
    watchdog = None
    watchdog_lost = False
    storage_cleaned = False
    if config.get('recovery_root') is not None:
        from diaktoros import native_recovery
        receipt = native_recovery.Record(Path(config['recovery_root']))
        try:
            if receipt.read()['state'] != 'armed':
                raise CleanupIncomplete('guardian requires an armed ownership record')
        except BaseException:
            receipt.close()
            raise
    process = None
    timed_out = False
    parent_lost = False
    leader_exited = False
    # SIG_IGN can survive exec and auto-reap children, invalidating the reserved
    # group ID. This trusted helper must own a waitable leader until cleanup.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        with closing(select.kqueue()) as events:
            events.control([select.kevent(lifeline_fd, filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_ADD)], 0, 0)
            # A fork-only host child can retain a copy of the lifeline writer.
            # Also watch our actual parent, checking PPID around registration so
            # startup after reparenting/PID reuse cannot target an unrelated PID.
            if owner <= 1 or os.getppid() != owner:
                parent_lost = True
            else:
                try:
                    events.control([select.kevent(owner, filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                    parent_lost = True
                if os.getppid() != owner:
                    parent_lost = True
            # Do not launch new work if the owner died during watchdog startup.
            if parent_lost or events.control(None, 2, 0):
                parent_lost = True
            else:
                if receipt is None:
                    process = subprocess.Popen(argv, env=env, cwd=cwd, close_fds=True,
                                               start_new_session=True)
                else:
                    from diaktoros import native_anchor
                    anchor_config = json.dumps({'argv': argv, 'env': env, 'cwd': cwd,
                                                'owner': os.getpid(), 'timeout': timeout,
                                                'turn_id': receipt.read()['turn_id']}).encode()
                    with tempfile.TemporaryFile() as source:
                        source.write(anchor_config)
                        source.seek(0)
                        handshake = config['anchor_handshake']
                        process = subprocess.Popen(
                            [str(Path(sys.executable).resolve()), '-I', '-B',
                             str(Path(native_anchor.__file__).resolve()),
                             str(source.fileno()), str(handshake)],
                            pass_fds=(source.fileno(), handshake), close_fds=True,
                            start_new_session=True,
                            env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
                    os.close(handshake)
                if receipt is not None:
                    watchdog = subprocess.Popen(
                        [str(Path(sys.executable).resolve()), '-I', '-B',
                         str(Path(__file__).resolve()), '--sentinel', str(os.getpid()),
                         str(timeout)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                        env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
                    receipt.update('running', guardian=os.getpid(), leader=process.pid,
                                   watchdog=watchdog.pid)
                    try:
                        events.control([select.kevent(watchdog.pid, filter=select.KQ_FILTER_PROC,
                                                      flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                      fflags=select.KQ_NOTE_EXIT)], 0, 0)
                    except ProcessLookupError:
                        watchdog_lost = True
                # No wait/poll before group cleanup: the unreaped child reserves
                # its PID, preventing a stale group ID from targeting a reused PID.
                try:
                    events.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                    leader_exited = True
                    # A very short-lived child can exit before registration.
                    # It has not been reaped; cleanup still precedes wait().
                else:
                    deadline = time.monotonic() + timeout
                    while not watchdog_lost:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        ready = events.control(None, 4, remaining)
                        if not ready:
                            timed_out = True
                            break
                        leader_exited = any(event.filter == select.KQ_FILTER_PROC and
                                            event.ident == process.pid for event in ready)
                        if any(event.filter == select.KQ_FILTER_READ or
                               (event.filter == select.KQ_FILTER_PROC and event.ident == owner)
                               for event in ready):
                            parent_lost = True
                            break
                        if leader_exited:
                            break
                        if watchdog is not None and any(
                                event.filter == select.KQ_FILTER_PROC and event.ident == watchdog.pid
                                for event in ready):
                            # The monitor can exit when its timer expires.
                            # Neither requires a stale PID
                            # signal: the guardian still owns the unreaped leader.
                            if time.monotonic() >= deadline:
                                timed_out = True
                            else:
                                watchdog_lost = True
                            break
    finally:
        try:
            if watchdog is not None:
                # The monitor is our unreaped child, not a discovered process.
                # Do not reap it before the signal, preserving its PID identity.
                try:
                    watchdog.terminate()
                except ProcessLookupError:
                    pass
                watchdog.wait(timeout=5)
            if process is not None:
                _drain_group(process.pid, leader_exited)
                if receipt is not None:
                    receipt.update('drained', cause=('watchdog_lost' if watchdog_lost else
                                                    'owner_lost' if parent_lost else
                                                    'timeout' if timed_out else 'leader_exit'))
                process.wait()
            elif receipt is not None:
                receipt.update('drained', cause='owner_lost_before_launch')
            if receipt is not None and (parent_lost or watchdog_lost):
                receipt.cleanup()
                storage_cleaned = True
        finally:
            if receipt is not None:
                receipt.close()
    result = {'returncode': process.returncode if process is not None else None,
              'timeout': timed_out, 'parent_lost': parent_lost, 'group_drained': True}
    if config.get('recovery_root') is not None:
        result.update(watchdog_lost=watchdog_lost, storage_cleaned=storage_cleaned)
    try:
        os.write(status_fd, json.dumps(result).encode())
    except BrokenPipeError:
        pass  # Owner death closed the status reader; group cleanup already ran.
    finally:
        os.close(status_fd)
        os.close(lifeline_fd)


def _completion(process, descriptor, *, allow_owner_loss=False):
    # The helper has exited. A host fork may still hold a status writer, so
    # missing completion must fail closed rather than block waiting for EOF.
    os.set_blocking(descriptor, False)
    try:
        status = os.read(descriptor, MAX_STATUS + 1)
    except BlockingIOError:
        status = b''
    try:
        record = json.loads(status)
        valid = (process.returncode == 0 and len(status) <= MAX_STATUS and
                 type(record['returncode']) is int and
                 type(record['timeout']) is bool and type(record['parent_lost']) is bool and
                 record['group_drained'] is True and
                 (allow_owner_loss or record['parent_lost'] is False))
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise CleanupIncomplete('native watchdog completion could not be verified; '
                                f'helper={process.returncode}, status={status!r}')
    if record.get('watchdog_lost') is True:
        if record.get('storage_cleaned') is not True:
            raise CleanupIncomplete('guardian storage cleanup could not be verified')
        raise WatchdogLost('native watchdog died; guardian drained the group and disposed storage')
    return record


def capture(argv: list[str], *, env: dict[str, str], timeout: int,
            cwd: Path | None = None, recovery_root: Path | None = None) -> subprocess.CompletedProcess:
    """Capture through a trusted watchdog; no sandbox is provided by this function.

    Caller must pass an already-contained command. No unsupervised fallback.
    The helper is outside the child's Seatbelt profile, in a separate session,
    with a scrubbed environment and exclusively host-owned pipe descriptors.
    """
    from . import contained
    if sys.platform != 'darwin':
        raise contained.ContainmentUnavailable('native watchdog requires macOS kqueue')
    if type(timeout) is not int or timeout < 1:
        raise ValueError('watchdog timeout must be positive')
    channel = peer = proof = None
    turn_id = None
    config = json.dumps({'argv': argv, 'env': env, 'timeout': timeout,
                         'cwd': str(cwd) if cwd is not None else None,
                         'owner': os.getpid()}).encode()
    if recovery_root is not None:
        from . import native_recovery
        with native_recovery.Record(recovery_root) as receipt:
            record = receipt.read()
            if record['state'] != 'armed':
                raise CleanupIncomplete('capture requires an armed ownership receipt')
            turn_id = record['turn_id']
        channel, peer = socket.socketpair()
        config = json.dumps({**json.loads(config), 'recovery_root': str(recovery_root),
                             'anchor_handshake': peer.fileno()}).encode()
    if len(config) > MAX_CONFIG:
        for connection in (channel, peer):
            if connection is not None:
                connection.close()
        raise ValueError('native watchdog configuration exceeds limit')
    with tempfile.TemporaryFile() as source:
        source.write(config)
        source.seek(0)
        life_read, life_write = os.pipe()
        status_read, status_write = os.pipe()
        process = None
        try:
            process = subprocess.Popen(
                [str(Path(sys.executable).resolve()), '-I', '-B', str(Path(__file__).resolve()),
                 str(source.fileno()), str(life_read), str(status_write)],
                pass_fds=(source.fileno(), life_read, status_write) +
                         ((peer.fileno(),) if peer is not None else ()),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
            os.close(life_read)
            life_read = None
            os.close(status_write)
            status_write = None
            if peer is not None:
                peer.close()
                peer = None
                try:
                    proof = _AnchorProof(channel, process, turn_id)
                except Exception as error:
                    channel.close()
                    channel = None
                    try:
                        os.write(life_write, b'x')
                    except BrokenPipeError:
                        pass
                    try:
                        process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass  # Never kill the independent cleanup owner.
                    raise CleanupIncomplete('native anchor registration failed; retain storage') from error
                channel.close()
                channel = None

            def completion(*, allow_owner_loss=False):
                try:
                    return _completion(process, status_read, allow_owner_loss=allow_owner_loss)
                except CleanupIncomplete:
                    if proof is not None:
                        try:
                            proof.recover(recovery_root, turn_id)
                        except GuardianLost:
                            raise
                        except (OSError, RuntimeError) as error:
                            raise CleanupIncomplete('guardian-loss cleanup remains unverified: '
                                                    + str(error)) from error
                    raise

            def abort():
                nonlocal life_write
                if life_write is not None:
                    # An explicit request also works if a forked host process
                    # retains another writer while this supervisor stays alive.
                    try:
                        os.write(life_write, b'x')
                    except BrokenPipeError:
                        pass
                    os.close(life_write)
                    life_write = None
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    # Do not kill the independent cleanup owner or pretend it
                    # finished. Caller must preserve storage for recovery.
                    raise CleanupIncomplete('native watchdog cleanup did not complete') from error
                completion(allow_owner_loss=True)

            result = contained.capture_process(process, argv=argv, timeout=timeout + 10,
                                               abort=abort)
            try:
                record = completion()
            except CleanupIncomplete as error:
                raise CleanupIncomplete(str(error) + '; ' + result.stderr[-3000:]) from error
            if record['timeout']:
                raise subprocess.TimeoutExpired(argv, timeout, result.stdout.encode(),
                                                result.stderr.encode())
            return subprocess.CompletedProcess(argv, record['returncode'],
                                               result.stdout, result.stderr)
        finally:
            if proof is not None:
                proof.close()
            for connection in (channel, peer):
                if connection is not None:
                    connection.close()
            for descriptor in (life_read, life_write, status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)


if __name__ == '__main__':
    # Only the trusted host helper imports this repository. Isolated Python
    # otherwise omits the script directory; no writable turn path is added.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.argv[1] == '--sentinel':
        _sentinel(*(int(value) for value in sys.argv[2:]))
    else:
        _watch(*(int(value) for value in sys.argv[1:]))
