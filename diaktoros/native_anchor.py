"""Trusted original-group leader for restricted native recovery turns only."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time


def run(config_fd, handshake_fd):
    from diaktoros.native_lifecycle import MAX_CONFIG
    with os.fdopen(config_fd, 'rb') as source:
        raw = source.read(MAX_CONFIG + 1)
    if len(raw) > MAX_CONFIG:
        raise ValueError('anchor configuration exceeds limit')
    config = json.loads(raw)
    owner = config['owner']
    # This helper is itself the group leader. Every destructive signal uses
    # our current group, never a journal PID or a discovered process identity.
    if os.getpid() != os.getpgrp():
        raise RuntimeError('native anchor must own its process group')
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    finished = False
    try:
        with closing(select.kqueue()) as events, socket.socket(fileno=handshake_fd) as channel:
            if owner <= 1 or os.getppid() != owner:
                raise RuntimeError('guardian lost before anchor launch')
            events.control([select.kevent(owner, filter=select.KQ_FILTER_PROC,
                                          flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                          fflags=select.KQ_NOTE_EXIT),
                            select.kevent(channel.fileno(), filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_ADD)], 0, 0)
            if os.getppid() != owner:
                raise RuntimeError('guardian lost during anchor registration')
            channel.sendall(json.dumps({'pid': os.getpid(), 'guardian': owner,
                                       'turn_id': config['turn_id']}).encode() + b'\n')
            # No seat launches until the host has registered our kernel exit
            # identity. Failure/EOF before acknowledgement kills only ourselves.
            ready = events.control(None, 2, 5)
            if (not ready or any(e.filter == select.KQ_FILTER_PROC for e in ready) or
                    channel.recv(1) != b'x' or os.getppid() != owner):
                raise RuntimeError('anchor launch acknowledgement lost')
            events.control([select.kevent(channel.fileno(), filter=select.KQ_FILTER_READ,
                                          flags=select.KQ_EV_DELETE)], 0, 0)
            channel.close()
            process = subprocess.Popen(config['argv'], env=config['env'], cwd=config['cwd'],
                                       close_fds=True, start_new_session=False)
            try:
                events.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                              fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except ProcessLookupError:
                pass  # Our unreaped child already exited.
            else:
                deadline = time.monotonic() + config['timeout'] + 1
                while True:
                    ready = events.control(None, 2, max(0, deadline - time.monotonic()))
                    if not ready or any(e.ident == owner for e in ready):
                        raise RuntimeError('guardian lost or anchor deadline reached')
                    if any(e.ident == process.pid for e in ready):
                        break
            code = process.wait()
            finished = True
            if code < 0:
                os.kill(os.getpid(), -code)
            return code
    finally:
        if not finished:
            os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(run(*(int(value) for value in sys.argv[1:])))
