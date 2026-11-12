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
import os
from pathlib import Path
import stat

from . import config

MAX_ENTRIES = 4096
MAX_CONFIG_BYTES = 1024 * 1024
# Worker-private subtrees that no Desktop view reads: isolated seat homes (tens of thousands
# of files while a turn runs), dependency checkouts and the CI cache. A loop keeps them
# directly in its state dir (gate.py, deps.py, ci_cache.py), so only there are they pruned:
# a child of a configured loop's state dir, its custom state_dir or else <state_root>/<loop>.
# The state dir is matched by identity (device and inode), not by spelling, so an alias with
# ".." or a symlink names the same directory and an unrelated directory that merely sits one
# level under the state root is not one. Such a directory is stamped but not descended. The
# same name anywhere else (the config dir, the gate_failures and seat_locks roots, deeper in
# a state dir) is descended like any other, and a scanned root is always descended whatever
# its name. Watching them only made every scan overflow, and an overflowing scan used to
# invalidate every second. The page's own polling still covers anything left unwatched.
PRUNED_DIRS = frozenset({"isolated-runs", "deps", "ci-cache"})
# At most one invalidation per MIN_GAP seconds: a burst of ledger writes during a turn is
# delivered once, on the leading edge, and anything after it on the trailing edge.
MIN_GAP = 5.0
# A scan that hit its cap cannot prove the unseen part is unchanged, so it still
# invalidates — but at the polling cadence, never once per scan.
OVERFLOW_GAP = 30.0


def _stamp(path: Path):
    """Metadata without following a symlink: (mode, device, inode, size, mtime, ctime)."""
    try:
        info = path.lstat()
        return (info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns)
    except FileNotFoundError:
        return ("missing",)
    except OSError:
        return ("unavailable",)


def _loop_configs(config_dir: Path, state_root: Path):
    """(custom state_dir roots, every configured loop's state dir, overflow) from config JSON.

    Only the state_dir key is read; credential paths mentioned there are never opened. A loop
    without a state_dir uses its default, <state_root>/<id>. Which files are loops is the
    loader's rule (config.config_file_id), never a second filter here.
    """
    custom, state_dirs, overflow = set(), set(), False
    config_bytes = MAX_CONFIG_BYTES
    try:
        with os.scandir(config_dir) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    overflow = True
                    break
                path = Path(entry.path)
                # The loader's own rule for which files are loops: a hidden .legacy.json is one.
                loop_id = config.config_file_id(entry.name)
                if loop_id is None or path.is_symlink():
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
                except (OSError, ValueError):
                    continue
                raw = loop.get("state_dir") if isinstance(loop, dict) else None
                if isinstance(raw, str) and raw:
                    custom.add(Path(raw).expanduser())
                    state_dirs.add(Path(raw).expanduser())
                elif isinstance(loop, dict) and not raw:
                    state_dirs.add(state_root / loop_id)
    except OSError:
        pass
    return custom, state_dirs, overflow


def _identities(paths):
    """The (device, inode) of each existing directory; one that cannot be read adds nothing."""
    found = set()
    for path in paths:
        try:
            info = os.stat(path)
        except (OSError, ValueError):
            continue
        if stat.S_ISDIR(info.st_mode):
            found.add((info.st_dev, info.st_ino))
    return found


class FileSnapshot:
    """Capture fixed host files and configured state roots; never follow state symlinks.

    Config JSON is read only to discover loop state dirs. Credential paths
    mentioned there are never opened. Worker-private subtrees (PRUNED_DIRS) directly in a
    loop's state dir are stamped but not descended. Work is capped; overflow is reported so
    the watcher can still invalidate at OVERFLOW_GAP rather than claim an incomplete scan is
    unchanged.
    """

    def __init__(self, home: Path, config_dir: Path):
        self.home, self.config_dir = home, config_dir

    def __call__(self):
        result = []
        remaining = MAX_ENTRIES
        roots = {config.host_path(key, self.home) for key in
                 ("state_root", "gate_failures", "seat_locks")}
        # Filled before any visit: the custom roots, and every loop state dir by identity.
        custom, state_dirs, overflow = _loop_configs(
            self.config_dir, config.host_path("state_root", self.home))
        loop_ids = _identities(state_dirs)

        def visit(root, recursive=True):
            nonlocal remaining, overflow
            pending = [(root, False)]   # (path, whether its parent is a loop's state dir)
            while pending:
                if remaining <= 0:
                    overflow = True
                    return
                path, in_loop_dir = pending.pop()
                remaining -= 1
                stamp = _stamp(path)
                result.append((str(path), stamp))
                # A root itself is always descended, whatever its name.
                pruned = in_loop_dir and path != root and path.name in PRUNED_DIRS
                if (recursive and isinstance(stamp[0], int) and stat.S_ISDIR(stamp[0])
                        and not pruned):
                    is_loop_dir = stamp[1:3] in loop_ids
                    try:
                        # scandir avoids materializing an unbounded directory listing.
                        with os.scandir(path) as entries:
                            for entry in entries:
                                if len(pending) >= remaining:
                                    overflow = True
                                    break
                                pending.append((Path(entry.path), is_loop_dir))
                    except OSError:
                        result.append((str(path), ("unavailable",)))

        roots |= custom
        for key in ("ledger", "pacing", "runtime"):
            path = config.host_path(key, self.home)
            visit(path, False)
            if key == "ledger":
                visit(path.with_name(path.name + "-wal"), False)
                # -shm is intentionally excluded: readers can alter it without a commit.
        for name in ("config.yaml", "hooks.json"):
            visit(self.home / name, False)
        visit(self.config_dir)
        for root in sorted(roots):
            visit(root)
        return tuple(sorted(result, key=lambda item: item[0])), overflow


class DesktopEvents:
    """One scanner, max-one pending invalidation per subscriber, no idle task.

    Invalidations are rate-limited: at most one per ``min_gap`` seconds (a change inside
    the gap is held and sent when it ends, never dropped), and a capped scan with no
    visible change invalidates once per ``overflow_gap``.
    """

    def __init__(self, snapshot, interval=1.0, min_gap=MIN_GAP, overflow_gap=OVERFLOW_GAP):
        self.snapshot = snapshot
        self.interval = interval
        self.min_gap = min_gap
        self.overflow_gap = overflow_gap
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
                    task = self.task
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    # Keep the task visible to for_app while its scan is joining.
                    # The watcher never takes this lock; reconnects wait here until
                    # cleanup has joined it and can safely start a fresh scanner.
                    self.task = None

    async def _watch(self):
        previous = None
        pending = False
        last_sent = None
        overflow_since = None
        clock = asyncio.get_running_loop().time
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
            now = clock()
            if not overflow:
                overflow_since = None
            elif overflow_since is None:
                overflow_since = now
            if previous is not None:
                if current != previous:
                    pending = True
                elif overflow and overflow_since is not None and now - overflow_since >= self.overflow_gap:
                    pending = True
                    overflow_since = now
            if pending and (last_sent is None or now - last_sent >= self.min_gap):
                pending = False
                last_sent = now
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
    # Retain active and closing managers: a reconnect must share the same lock
    # until the last subscriber has joined its in-flight scanner thread.
    for old in list(managers):
        manager = managers[old]
        if not manager.subscribers and manager.task is None and not manager._lock.locked():
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
