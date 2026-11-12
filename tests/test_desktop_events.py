"""Real filesystem/SQLite and ASGI socket lifecycle regressions for #647."""
import _home_guard  # noqa: F401
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from diaktoros import desktop_events as events
from hermes_prereqs import skip_or_fail


def require_socket_prereqs(test):
    """Standalone shards stay stdlib-only; the pinned-Hermes lane must run this test."""
    for module in ("fastapi", "httpx", "hermes_cli"):
        if importlib.util.find_spec(module) is None:
            skip_or_fail(test, f"desktop socket integration needs {module} on this interpreter")
    # Do not catch import errors below: broken/transitive imports must fail, not skip.


class SocketPrerequisites(unittest.TestCase):
    def test_missing_prerequisite_skips_standalone(self):
        for missing in ("fastapi", "httpx", "hermes_cli"):
            with self.subTest(missing=missing), \
                    mock.patch("hermes_prereqs.REQUIRED", False), \
                    mock.patch.object(importlib.util, "find_spec",
                                      side_effect=lambda name: None if name == missing else object()):
                with self.assertRaisesRegex(unittest.SkipTest, missing):
                    Socket().test_real_hermes_validator_plugin_route_refusal_disconnect_reconnect()

    def test_missing_prerequisite_fails_required_lane(self):
        for missing in ("fastapi", "httpx", "hermes_cli"):
            with self.subTest(missing=missing), \
                    mock.patch("hermes_prereqs.REQUIRED", True), \
                    mock.patch.object(importlib.util, "find_spec",
                                      side_effect=lambda name: None if name == missing else object()):
                with self.assertRaisesRegex(AssertionError, f"DIAKTOROS_REQUIRE_HERMES_SOURCE=1.*{missing}"):
                    Socket().test_real_hermes_validator_plugin_route_refusal_disconnect_reconnect()

    def test_available_prerequisites_do_not_skip_either_mode(self):
        for required in (False, True):
            with self.subTest(required=required), \
                    mock.patch("hermes_prereqs.REQUIRED", required), \
                    mock.patch.object(importlib.util, "find_spec", return_value=object()):
                require_socket_prereqs(self)


class Files(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.directory = self.home / "diaktoros.d"
        self.directory.mkdir()
        self.state = self.home / "custom-state"
        self.state.mkdir()
        (self.directory / "demo.json").write_text(json.dumps({"state_dir": str(self.state),
            "tokens": {"reader": str(self.home / "secret")}}))
        (self.home / "secret").write_text("NEVER READ ME")
        self.ledger = self.home / "state" / "diaktoros-runs.sqlite"
        self.ledger.parent.mkdir()
        self.db = sqlite3.connect(self.ledger)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE turns (id INTEGER)")
        self.db.commit()
        self.snapshot = events.FileSnapshot(self.home, self.directory)
        self.manager = events.DesktopEvents(self.snapshot, interval=.01, min_gap=.2)

    async def settled(self):
        await asyncio.sleep(.06)

    async def test_unchanged_real_ledger_and_state_are_silent(self):
        before = self.snapshot()
        async with self.manager.subscribe() as queue:
            await self.settled()
            self.assertTrue(queue.empty())
            self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.manager.task)
        self.assertFalse(self.manager.subscribers)

    async def test_external_wal_commit_state_config_changes_are_coalesced(self):
        async with self.manager.subscribe() as queue:
            await self.settled()
            before = self.snapshot()
            self.db.execute("INSERT INTO turns VALUES (1)")
            self.db.commit()
            self.assertNotEqual(before, self.snapshot())
            message = await asyncio.wait_for(queue.get(), 1)
            self.assertEqual(message, {"event": "invalidate", "version": 1})
            for n in range(10):
                (self.state / "hold.json").write_text(str(n))
                (self.directory / "new.json").write_text("{}")
                await asyncio.sleep(.015)
            # The burst is held for the rest of min_gap, then delivered once — never dropped.
            await asyncio.sleep(.3)
            self.assertEqual(queue.qsize(), 1)
            self.assertNotIn(str(self.home), json.dumps(message))
            self.assertNotIn("NEVER READ", json.dumps(message))

    async def test_one_task_shared_disconnect_then_reconnect(self):
        async with self.manager.subscribe() as first:
            task = self.manager.task
            async with self.manager.subscribe() as second:
                self.assertIs(task, self.manager.task)
                await self.settled()
                (self.state / "status.json").write_text("changed")
                self.assertEqual(await asyncio.wait_for(first.get(), 1),
                                 await asyncio.wait_for(second.get(), 1))
            self.assertFalse(task.done())
        self.assertTrue(task.done())
        async with self.manager.subscribe():
            self.assertIsNot(task, self.manager.task)
        self.assertIsNone(self.manager.task)

    async def test_missing_unreadable_and_recovery_do_not_spin_or_leak(self):
        absent = events.FileSnapshot(self.home / "absent", self.home / "absent-config")
        self.assertEqual(absent(), absent())
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("private path")):
            self.assertEqual(self.snapshot(), self.snapshot())
        with mock.patch.object(self.manager, "snapshot", side_effect=OSError("secret")):
            async with self.manager.subscribe() as queue:
                await self.settled()
                self.assertTrue(queue.empty())
        async with self.manager.subscribe() as queue:
            await self.settled()
            self.ledger.unlink()
            await asyncio.wait_for(queue.get(), 1)
        self.assertIsNone(self.manager.task)

    async def test_cancelling_subscriber_cleans_up(self):
        entered = asyncio.Event()
        async def consumer():
            async with self.manager.subscribe():
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(consumer())
        await entered.wait()
        scanner = self.manager.task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(scanner.done())
        self.assertFalse(self.manager.subscribers)
        self.assertIsNone(self.manager.task)

    async def test_shutdown_cancels_socket_tasks_and_scanner(self):
        incoming = asyncio.Queue()
        sent = asyncio.Event()
        class Connection:
            app = object()
            async def accept(self):
                pass
            async def send_json(self, message):
                sent.set()
            async def receive(self):
                return await incoming.get()
        # The route's admission is covered separately against the real validator.
        auth = types.ModuleType("hermes_cli.web_server_chat")
        auth._ws_auth_ok = lambda ws: True
        with mock.patch.dict(sys.modules, {"hermes_cli.web_server_chat": auth}), \
                mock.patch.object(events, "for_app", return_value=self.manager):
            task = asyncio.create_task(events.serve(Connection()))
            await asyncio.wait_for(sent.wait(), 1)
            scanner = self.manager.task
            self.assertIsNotNone(scanner)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(scanner.done())
            self.assertIsNone(self.manager.task)
            self.assertFalse(self.manager.subscribers)
            self.assertFalse([t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                              and not t.done()])

    async def test_never_opens_credentials_or_sqlite(self):
        original = os.open
        opened = []
        def opening(path, *args, **kwargs):
            opened.append(path)
            return original(path, *args, **kwargs)
        # A corrupt config store's special files must never stall scanner shutdown.
        os.mkfifo(self.directory / 'pipe.json')
        (self.directory / 'credential.json').symlink_to(self.home / 'secret')
        with mock.patch.object(os, "open", opening), mock.patch.object(sqlite3, "connect", side_effect=AssertionError):
            self.snapshot()
        self.assertEqual(set(opened), {self.directory / "demo.json", self.directory / 'pipe.json'})

    async def test_inflight_scan_is_joined_before_last_disconnect_returns(self):
        import threading
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow_snapshot():
            started.set()
            release.wait(2)
            finished.set()
            return (), False
        self.manager.snapshot = slow_snapshot
        async def consumer():
            async with self.manager.subscribe():
                await asyncio.Event().wait()
        task = asyncio.create_task(consumer())
        try:
            await asyncio.to_thread(started.wait, 1)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done(), 'disconnect must join the in-flight scan')
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertIsNone(self.manager.task)
        self.assertFalse(self.manager.subscribers)

    async def test_for_app_reconnect_waits_for_closing_scanner_join(self):
        import threading
        started, release = threading.Event(), threading.Event()
        calls = []
        def blocked_snapshot():
            calls.append(None)
            if len(calls) == 1:
                started.set()
                if not release.wait(5):
                    raise AssertionError("test did not release the blocked snapshot")
            return (), False
        app = types.SimpleNamespace(state=types.SimpleNamespace())
        reconnected = asyncio.Event()
        async def consumer(manager, entered=None):
            async with manager.subscribe():
                if entered is not None:
                    entered.set()
                await asyncio.Event().wait()
        with mock.patch.object(events.config, "home", return_value=self.home), \
                mock.patch.object(events.config, "config_dir", return_value=self.directory), \
                mock.patch.object(events, "FileSnapshot", return_value=blocked_snapshot):
            manager = events.for_app(app)
            first = asyncio.create_task(consumer(manager))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                scanner = manager.task
                first.cancel()
                async def cancellation_started():
                    while not scanner.cancelling():
                        await asyncio.sleep(0)
                await asyncio.wait_for(cancellation_started(), 1)
                self.assertFalse(first.done())
                self.assertFalse(manager.subscribers)
                replacement = events.for_app(app)
                self.assertIs(replacement, manager, "registry must retain a joining manager")
                self.assertIs(manager.task, scanner, "task stays discoverable until joined")
                self.assertTrue(manager._lock.locked())
                second = asyncio.create_task(consumer(replacement, reconnected))
                # FIFO loop scheduling lets the reconnect attempt acquire the lock.
                await asyncio.sleep(0)
                self.assertFalse(reconnected.is_set())
                self.assertEqual(len(calls), 1)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, 1)
                await asyncio.wait_for(reconnected.wait(), 1)
                self.assertTrue(scanner.done())
                self.assertIsNot(manager.task, scanner)
                self.assertIs(events.for_app(app), manager)
            finally:
                release.set()
                for task in (first, second):
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertIsNone(manager.task)
            self.assertFalse(manager.subscribers)
            # Fully idle managers can still be evicted; the registry stays bounded.
            self.assertIsNot(events.for_app(app), manager)

    async def test_bounded_overflow_conservatively_invalidates(self):
        manager = events.DesktopEvents(self.snapshot, interval=.01, overflow_gap=.05)
        with mock.patch.object(events, "MAX_ENTRIES", 2):
            self.assertTrue(self.snapshot()[1])
            async with manager.subscribe() as queue:
                await asyncio.wait_for(queue.get(), 1)
                self.assertLessEqual(queue.qsize(), 1)

    async def test_overflow_alone_invalidates_at_overflow_gap_not_every_scan(self):
        # The old watcher sent one invalidation per one-second scan while capped, so every
        # Desktop read refetched every second. An unchanged capped scan now waits the gap.
        manager = events.DesktopEvents(self.snapshot, interval=.01, min_gap=0, overflow_gap=.4)
        with mock.patch.object(events, "MAX_ENTRIES", 2):
            async with manager.subscribe() as queue:
                await asyncio.sleep(.2)
                self.assertTrue(queue.empty(), "a capped, unchanged scan must not invalidate per scan")
                await asyncio.wait_for(queue.get(), 1)

    async def test_worker_private_subtrees_are_stamped_not_descended(self):
        runs = self.state / "isolated-runs" / "turn-abc" / "home"
        runs.mkdir(parents=True)
        for n in range(50):
            (runs / f"f{n}").write_text("x")
        for name in ("deps", "ci-cache"):
            (self.state / name / "nested").mkdir(parents=True)
            (self.state / name / "nested" / "file").write_text("x")
        paths = [path for path, _ in self.snapshot()[0]]
        self.assertIn(str(self.state / "isolated-runs"), paths)
        for name in events.PRUNED_DIRS:
            self.assertFalse([p for p in paths if p.startswith(str(self.state / name) + os.sep)], name)
        self.assertFalse(self.snapshot()[1])
        before = self.snapshot()
        (runs / "f0").write_text("rewritten by a running seat")
        (runs / "new").write_text("y")
        self.assertEqual(before, self.snapshot())
        # A file a Desktop view does read is still watched.
        (self.state / "watchdog.json").write_text("{}")
        self.assertNotEqual(before, self.snapshot())

    async def test_default_state_dir_worker_trees_are_pruned(self):
        # The default state dir is <state_root>/<loop>: its worker trees are pruned like a
        # custom state_dir's (#2146).
        (self.directory / "plain.json").write_text(json.dumps({"tokens": {}}))
        loop = self.home / "state" / "diaktoros" / "plain"
        for name in events.PRUNED_DIRS:
            (loop / name / "nested").mkdir(parents=True)
            (loop / name / "nested" / "file").write_text("x")
        paths = [path for path, _ in self.snapshot()[0]]
        for name in events.PRUNED_DIRS:
            self.assertIn(str(loop / name), paths)
            self.assertFalse([p for p in paths if p.startswith(str(loop / name) + os.sep)], name)
        before = self.snapshot()
        for name in events.PRUNED_DIRS:
            (loop / name / "nested" / "file").write_text("rewritten by a worker")
        self.assertEqual(before, self.snapshot())

    async def test_pruned_names_outside_a_loop_state_dir_are_descended(self):
        # The prune is where a loop creates those trees (directly in its state dir), not by
        # name under every scanned root: the same name elsewhere is watched (#2146).
        elsewhere = [self.directory / "deps",                                  # config dir
                     self.home / "state" / "diaktoros-gate-failures" / "isolated-runs",
                     self.home / "state" / "diaktoros-seat-locks" / "ci-cache",
                     self.home / "state" / "diaktoros" / "deps",               # the state root itself
                     self.state / "artifacts" / "ci-cache",                    # deeper in a state dir
                     self.home / "state" / "diaktoros" / "demo" / "x" / "isolated-runs"]
        for directory in elsewhere:
            (directory / "nested").mkdir(parents=True)
            (directory / "nested" / "file").write_text("x")
        paths = [path for path, _ in self.snapshot()[0]]
        for directory in elsewhere:
            with self.subTest(directory=directory.relative_to(self.home)):
                self.assertIn(str(directory / "nested" / "file"), paths)
                before = self.snapshot()
                (directory / "nested" / "file").write_text("changed")
                self.assertNotEqual(before, self.snapshot())

    def under(self, paths, directory):
        return [p for p in paths if p.startswith(str(directory) + os.sep)]

    def test_state_dir_spelled_with_dotdot_is_pruned_by_identity(self):
        # A state_dir under the config dir, spelled through "..": the config-dir walk reaches
        # the same directory under another spelling and must still prune its worker trees,
        # or a large checkout spends the whole scan budget and every scan overflows.
        custom, alias = self.directory / "custom", self.directory / "alias"
        deps = custom / "deps"
        deps.mkdir(parents=True)
        alias.mkdir()
        for n in range(events.MAX_ENTRIES + 100):
            (deps / f"f{n}").touch()
        for name, state_dir in (("other", alias / ".." / "custom"), ("gone", self.home / "missing")):
            (self.directory / f"{name}.json").write_text(json.dumps({"state_dir": str(state_dir)}))
        paths, overflow = self.snapshot()
        paths = [path for path, _ in paths]
        self.assertFalse(overflow)
        self.assertIn(str(deps), paths)
        self.assertFalse(self.under(paths, deps))
        self.assertFalse(self.under(paths, alias / ".." / "custom" / "deps"))
        self.assertIn(str(alias / ".." / "custom"), paths)   # the configured root is still scanned

    async def test_symlinked_state_dir_is_pruned_by_identity_and_never_followed(self):
        custom, link = self.directory / "custom", self.directory / "link"
        (custom / "deps" / "nested").mkdir(parents=True)
        (custom / "deps" / "nested" / "file").write_text("x")
        (custom / "watchdog.json").write_text("{}")
        link.symlink_to(custom, target_is_directory=True)
        (self.directory / "other.json").write_text(json.dumps({"state_dir": str(link)}))
        paths = [path for path, _ in self.snapshot()[0]]
        self.assertIn(str(custom / "deps"), paths)
        self.assertFalse(self.under(paths, custom / "deps"))
        self.assertIn(str(custom / "watchdog.json"), paths)
        self.assertIn(str(link), paths)
        self.assertFalse(self.under(paths, link), "the walk never follows a symlink")

    async def test_state_dir_equal_to_the_state_root_prunes_only_its_direct_children(self):
        # #2403: a custom state_dir that is the state root itself is one loop's state dir;
        # its subdirectories are not other loops' state dirs, so their deps are watched.
        root = self.home / "state" / "diaktoros"
        (self.directory / "demo.json").write_text(json.dumps({"state_dir": str(root)}))
        (root / "deps" / "nested").mkdir(parents=True)
        (root / "deps" / "nested" / "file").write_text("x")
        target = root / "artifacts" / "deps" / "watchdog.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}")
        before = self.snapshot()
        paths = [path for path, _ in before[0]]
        self.assertFalse(self.under(paths, root / "deps"))
        self.assertIn(str(target), paths)
        target.write_text('{"changed": true}')
        self.assertNotEqual(before, self.snapshot())

    def valid_loop(self, loop_id, **extra):
        seats = {seat: {"route": seat, "profile": "default"} for seat in ("reviewer", "fixer")}
        (self.directory / f"{loop_id}.json").write_text(json.dumps({
            "id": loop_id, "repo": "acme/widgets", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "read_token": "reader", "seats": seats, **extra}))

    def test_hidden_loop_the_loader_accepts_has_its_state_dir_watched(self):
        # #2389 review F2: the loader loads an existing .legacy.json (init only refuses new
        # leading-dot IDs), so its custom state_dir, outside every scanned root, is watched.
        legacy = self.home / "legacy-state"
        legacy.mkdir()
        target = legacy / "watchdog.json"
        target.write_text("{}")
        self.valid_loop(".legacy", state_dir=str(legacy))
        with mock.patch.object(events.config, "config_dir", return_value=self.directory):
            self.assertEqual(events.config.load_id(".legacy")["state_dir"], str(legacy))
        before = self.snapshot()
        self.assertFalse(before[1])
        self.assertIn(str(target), [path for path, _ in before[0]])
        target.write_text('{"changed": true}')
        self.assertNotEqual(before, self.snapshot())

    def test_watched_loops_are_exactly_the_loops_the_loader_loads(self):
        # One rule (config.config_file_id) for which config files are loops, not a parallel
        # filter in the watcher that drifts from the loader's glob and load_id.
        names = [".legacy", ".", "..", "", "a.b", "plain"]
        for loop_id in names:
            self.valid_loop(loop_id, state_dir=str(self.home / f"s{len(loop_id)}{loop_id}"))
        (self.directory / "upper.JSON").write_text("{}")
        (self.directory / "temp.json.tmp").write_text("{}")
        with mock.patch.object(events.config, "config_dir", return_value=self.directory):
            loaded = {loop["id"] for loop in events.config.readable_loops()[0]}
        self.assertEqual({".legacy", "a.b", "plain"}, loaded)
        # demo.json is named like a loop but its content is refused; its state_dir is still
        # read, as before. The names the loader refuses outright are never read.
        watched = {events.config.config_file_id(path.name) for path in self.directory.iterdir()}
        self.assertEqual(loaded | {"demo", None}, watched)

    async def test_burst_is_rate_limited_and_the_trailing_change_is_never_dropped(self):
        manager = events.DesktopEvents(self.snapshot, interval=.01, min_gap=.3)
        async with manager.subscribe() as queue:
            await self.settled()
            (self.state / "hold.json").write_text("1")
            await asyncio.wait_for(queue.get(), 1)
            (self.state / "hold.json").write_text("2")
            await asyncio.sleep(.1)
            self.assertTrue(queue.empty(), "a second change inside the gap is held")
            await asyncio.wait_for(queue.get(), 1)
            await asyncio.sleep(.4)
            self.assertTrue(queue.empty(), "one held change is delivered once")


class Socket(unittest.TestCase):
    def test_real_hermes_validator_plugin_route_refusal_disconnect_reconnect(self):
        require_socket_prereqs(self)
        # Use the real Hermes validator, NOT a guessed Depends(HTTP-only auth).
        # Stub only web_server globals to avoid starting/importing the live server.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        import hermes_cli.web_server_chat  # noqa: F401
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("dk_events_test_api", root / "dashboard/plugin_api.py")
        api = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {spec.name: api}):
            spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/diaktoros")
        server = types.ModuleType("hermes_cli.web_server")
        server.app = app
        server._SESSION_TOKEN = "fixture-only-session-token"
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        directory = home / 'diaktoros.d'
        directory.mkdir()
        state = home / 'custom-state'
        state.mkdir()
        (directory / 'demo.json').write_text(json.dumps({'state_dir': str(state)}))
        ledger = home / 'state' / 'diaktoros-runs.sqlite'
        ledger.parent.mkdir()
        db = sqlite3.connect(ledger)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE turns (id INTEGER)')
        db.commit()
        manager = events.DesktopEvents(events.FileSnapshot(home, directory), interval=.01)
        disconnected = threading.Event()

        async def observed_app(scope, receive, send):
            try:
                await app(scope, receive, send)
            finally:
                if scope['type'] == 'websocket':
                    disconnected.set()

        with mock.patch.dict(sys.modules, {"hermes_cli.web_server": server}), \
                mock.patch.object(events, "for_app", return_value=manager), TestClient(observed_app) as client:
            for suffix in ("", "?token=wrong"):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/api/plugins/diaktoros/events" + suffix):
                        self.fail("unauthenticated socket accepted")
                self.assertIsNone(manager.task)
            # HERMES_PINNED validates browser tickets in gated mode; the newer native
            # X-Hermes-Gateway-Ticket header is not part of that commit's contract.
            app.state.auth_required = True
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/api/plugins/diaktoros/events?ticket=invalid"):
                    self.fail("invalid gated ticket accepted")
            self.assertIsNone(manager.task)
            app.state.auth_required = False
            for _ in range(2):
                disconnected.clear()
                with client.websocket_connect("/api/plugins/diaktoros/events?token=fixture-only-session-token") as ws:
                    self.assertEqual(ws.receive_json(), {"event": "invalidate", "version": 1})
                    scanner = manager.task
                    self.assertIsNotNone(scanner)
                    db.execute('INSERT INTO turns VALUES (1)')
                    db.commit()
                    self.assertEqual(ws.receive_json(), {"event": "invalidate", "version": 1})
                    (state / 'hold.json').write_text('{"held": true}')
                    self.assertEqual(ws.receive_json(), {"event": "invalidate", "version": 1})
                    # TestClient.__exit__ sends disconnect then immediately cancels
                    # its AnyIO scope: that races the route's async finally/join.
                    # Observe graceful ASGI completion BEFORE permitting scope cancel,
                    # not merely zero subscribers while the scanner is still joining.
                    ws.close()
                    self.assertTrue(disconnected.wait(5), 'socket route did not finish teardown')
                    self.assertTrue(scanner.done())
                self.assertIsNone(manager.task)
                self.assertFalse(manager.subscribers)


if __name__ == "__main__":
    unittest.main()
