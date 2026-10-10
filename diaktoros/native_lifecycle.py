"""Experimental independent watchdog for a native turn's original process group.

The host-only lifeline never reaches the sandboxed executable. EOF, timeout,
and normal leader exit all kill the original group before reaping its leader.
Detached descendants remain an explicit production blocker. This is not a
general descendant tracker, nor recovery after watchdog/host-machine death.
"""
from __future__ import annotations

from contextlib import closing
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

MAX_CONFIG = 1024 * 1024
MAX_STATUS = 1024


class CleanupIncomplete(RuntimeError):
    """Watchdog completion cannot be verified; storage must be retained."""


def _watch(config_fd: int, lifeline_fd: int, status_fd: int):
    # Configuration comes from a trusted anonymous file, not child arguments or state.
    with os.fdopen(config_fd, 'rb') as source:
        config = json.loads(source.read(MAX_CONFIG + 1))
    argv, env, timeout, cwd = (config[k] for k in ('argv', 'env', 'timeout', 'cwd'))
    process = None
    timed_out = False
    parent_lost = False
    try:
        with closing(select.kqueue()) as events:
            events.control([select.kevent(lifeline_fd, filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_ADD)], 0, 0)
            # Do not launch new work if the owner died during watchdog startup.
            if events.control(None, 1, 0):
                parent_lost = True
            else:
                process = subprocess.Popen(argv, env=env, cwd=cwd, close_fds=True,
                                           start_new_session=True)
                # No wait/poll before group cleanup: the unreaped child reserves
                # its PID, preventing a stale group ID from targeting a reused PID.
                try:
                    events.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                                  fflags=select.KQ_NOTE_EXIT)], 0, 0)
                except OSError as error:
                    if error.errno != errno.ESRCH:
                        raise
                    # A very short-lived child can exit before registration.
                    # It has not been reaped; cleanup still precedes wait().
                else:
                    deadline = time.monotonic() + timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        ready = events.control(None, 2, remaining)
                        if not ready:
                            timed_out = True
                            break
                        if any(event.filter == select.KQ_FILTER_READ for event in ready):
                            parent_lost = True
                            break
                        if any(event.filter == select.KQ_FILTER_PROC for event in ready):
                            break
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    result = {'returncode': process.returncode if process is not None else None,
              'timeout': timed_out, 'parent_lost': parent_lost}
    try:
        os.write(status_fd, json.dumps(result).encode())
    except BrokenPipeError:
        pass  # Owner death closed the status reader; group cleanup already ran.
    finally:
        os.close(status_fd)
        os.close(lifeline_fd)


def capture(argv: list[str], *, env: dict[str, str], timeout: int,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
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
    config = json.dumps({'argv': argv, 'env': env, 'timeout': timeout,
                         'cwd': str(cwd) if cwd is not None else None}).encode()
    if len(config) > MAX_CONFIG:
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
                pass_fds=(source.fileno(), life_read, status_write),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
                env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
            os.close(life_read)
            life_read = None
            os.close(status_write)
            status_write = None

            def abort():
                nonlocal life_write
                if life_write is not None:
                    os.close(life_write)
                    life_write = None
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    # Do not kill the independent cleanup owner or pretend it
                    # finished. Caller must preserve storage for recovery.
                    raise CleanupIncomplete('native watchdog cleanup did not complete') from error

            result = contained.capture_process(process, argv=argv, timeout=timeout + 10,
                                               abort=abort)
            status = os.read(status_read, MAX_STATUS + 1)
            try:
                record = json.loads(status)
                valid = (process.returncode == 0 and len(status) <= MAX_STATUS and
                         type(record['returncode']) is int and
                         type(record['timeout']) is bool and record['parent_lost'] is False)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise CleanupIncomplete('native watchdog completion could not be verified')
            if record['timeout']:
                raise subprocess.TimeoutExpired(argv, timeout, result.stdout.encode(),
                                                result.stderr.encode())
            return subprocess.CompletedProcess(argv, record['returncode'],
                                               result.stdout, result.stderr)
        finally:
            for descriptor in (life_read, life_write, status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)


if __name__ == '__main__':
    _watch(*(int(value) for value in sys.argv[1:]))
