"""Read-only, shared Desktop invalidations for changes made by external workers.

No database connections, credentials, network calls or writes: metadata also sees
SQLite WAL commits without opening/creating the ledger. One bounded scan per app/home,
not per component. Notifications contain no filenames, contents or exception strings.
The host owns its lifespan (and does not enter plugin router lifespans), so socket
finally blocks own reference-counted teardown, including server cancellation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import stat

from . import config

MAX_ENTRIES = 4096
MAX_CONFIG_BYTES = 1024 * 1024


def _stamp(path: Path):
    try:
        info = path.lstat()
        return (info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    except FileNotFoundError:
        return ("missing",)
    except OSError:
        return ("unavailable",)


class FileSnapshot:
    """Capture fixed host files and configured state roots; never follow state symlinks.

    Config JSON is read only to discover custom state_dir roots. Credential paths
    mentioned there are never opened. Work is capped; overflow conservatively
    invalidates each interval rather than claiming an incomplete scan is unchanged.
    """

    def __init__(self, home: Path, config_dir: Path):
        self.home, self.config_dir = home, config_dir

    def __call__(self):
        result = []
        remaining = MAX_ENTRIES
        overflow = False

        def visit(root, recursive=True):
            nonlocal remaining, overflow
            pending = [root]
            while pending:
                if remaining <= 0:
                    overflow = True
                    return
                path = pending.pop()
                remaining -= 1
                stamp = _stamp(path)
                result.append((str(path), stamp))
                if recursive and isinstance(stamp[0], int) and stat.S_ISDIR(stamp[0]):
                    try:
                        # scandir avoids materializing an unbounded directory listing.
                        import os
                        with os.scandir(path) as entries:
                            for entry in entries:
                                if len(pending) >= remaining:
                                    overflow = True
                                    break
                                pending.append(Path(entry.path))
                    except OSError:
                        result.append((str(path), ("unavailable",)))

        roots = {config.host_path(key, self.home) for key in
                 ("state_root", "gate_failures", "seat_locks")}
        for key in ("ledger", "pacing", "runtime"):
            path = config.host_path(key, self.home)
            visit(path, False)
            if key == "ledger":
                visit(path.with_name(path.name + "-wal"), False)
                # -shm is intentionally excluded: readers can alter it without a commit.
        for name in ("config.yaml", "hooks.json"):
            visit(self.home / name, False)
        visit(self.config_dir)
        config_bytes = MAX_CONFIG_BYTES
        try:
            import os
            with os.scandir(self.config_dir) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_ENTRIES:
                        overflow = True
                        break
                    path = Path(entry.path)
                    if path.suffix != ".json" or path.is_symlink():
                        continue
                    try:
                        # A malformed store may contain FIFOs or race a rename. Never
                        # block on a special file or follow a credential symlink.
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, "rb") as stream:
                            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                continue
                            data = stream.read(config_bytes + 1)
                        config_bytes -= len(data)
                        if config_bytes < 0:
                            overflow = True
                            break
                        loop = json.loads(data)
                        raw = loop.get("state_dir") if isinstance(loop, dict) else None
                        if isinstance(raw, str) and raw:
                            roots.add(Path(raw).expanduser())
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass
        for root in sorted(roots):
            visit(root)
        return tuple(sorted(result, key=lambda item: item[0])), overflow


class DesktopEvents:
    """One scanner, max-one pending invalidation per subscriber, no idle task."""

    def __init__(self, snapshot, interval=1.0):
        self.snapshot = snapshot
        self.interval = interval
        self.subscribers = set()
        self.task = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    @asynccontextmanager
    async def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        async with self._lock:
            self.subscribers.add(queue)
            if self.task is None:
                self._ready = asyncio.Event()
                self.task = asyncio.create_task(self._watch(), name="diaktoros-desktop-events")
        try:
            # Establish the baseline before the initial reconnect invalidation:
            # REST reads triggered by it cannot race ahead of the baseline.
            await self._ready.wait()
            yield queue
        finally:
            async with self._lock:
                self.subscribers.discard(queue)
                if not self.subscribers and self.task is not None:
                    task, self.task = self.task, None
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _watch(self):
        previous = None
        while True:
            try:
                # Shield then join an in-flight scan on cancellation: reconnect must
                # never overlap the old scanner's thread or leave IO after teardown.
                scan = asyncio.create_task(asyncio.to_thread(lambda: self.snapshot()))
                try:
                    current, overflow = await asyncio.shield(scan)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await scan
                    raise
            except Exception:
                current, overflow = ("unavailable",), False
            if previous is not None and (current != previous or overflow):
                for queue in tuple(self.subscribers):
                    if queue.empty():
                        queue.put_nowait({"event": "invalidate", "version": 1})
            previous = current
            self._ready.set()
            await asyncio.sleep(self.interval)


def for_app(app):
    """Resolve paths once in the admitted request scope; no cross-home subscriptions."""
    home, directory = config.home(), config.config_dir()
    managers = getattr(app.state, "diaktoros_desktop_events", None)
    if managers is None:
        managers = app.state.diaktoros_desktop_events = {}
    key = (str(home), str(directory))
    # Retain only managers with subscribers; no unbounded registry across profiles.
    for old in list(managers):
        if not managers[old].subscribers:
            del managers[old]
    if key not in managers:
        managers[key] = DesktopEvents(FileSnapshot(home, directory))
    return managers[key]


async def serve(websocket):
    """Use Hermes's actual native-grant/token validator, before accepting or scanning."""
    try:
        from hermes_cli.web_server_chat import _ws_auth_ok
        admitted = _ws_auth_ok(websocket)
    except Exception:
        admitted = False
    if not admitted:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    async with for_app(websocket.app).subscribe() as queue:
        async def send():
            # Initial invalidation closes the reconnect gap before the first scan.
            await websocket.send_json({"event": "invalidate", "version": 1})
            while True:
                await websocket.send_json(await queue.get())

        async def receive():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
