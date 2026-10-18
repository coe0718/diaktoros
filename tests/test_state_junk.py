"""#545, #547, #548: the state files' writers tolerate junk the way their readers do (#80).

A hand edit, a torn write or an older writer can leave a value of the wrong shape in
``locks.json``, ``pending.json``, ``inflight.json`` or ``breach.json``. The readers already
treated such a value as absent; the writers raised on it, so one bad entry crashed every gate
run (and the watchdog re-drove it into the same crash). Every reader and writer here is run
against every junk shape: none may raise, and a write leaves a well-formed file behind.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import gate, state  # noqa: E402

HEAD = "a" * 40
KEY = "acme/widgets#7"
JUNK = ["junk-string", ["a", "b"], 7, None, True, {"x": "not-a-number"}, {KEY: "junk"},
        {KEY: ["x"]}, {KEY: {"at": "never"}}]


class Junk(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        loop = {"id": "junk", "repo": "acme/widgets", "base": "main", "cap": 3,
                "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                "state_dir": str(root / "state"), "fixers": ["f"], "reviewers": ["r"],
                "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)

    def plant(self, path, top_or_seat_value, *, seat_level=True):
        value = {"reviewer": top_or_seat_value} if seat_level else top_or_seat_value
        path.write_text(json.dumps(value))

    def test_lock_and_queue_writers_survive_every_shape(self):
        st = self.st
        calls = {
            "locks": [lambda: st.acquire("reviewer", KEY, HEAD, "why", run="r1"),
                      lambda: st.release_exact("reviewer", KEY, 1.0),
                      lambda: st.release_if("reviewer", KEY, HEAD, run="r1"),
                      lambda: st.release_all("reviewer"),
                      lambda: st.active("reviewer"), lambda: st.live_locks("reviewer")],
            "pending": [lambda: st.queue_add("reviewer", KEY, HEAD, "u", "busy"),
                        lambda: st.queue_replace_if("reviewer", KEY, None, HEAD, "u", "r"),
                        lambda: st.queue_items("reviewer"),
                        lambda: st.queue_pop("reviewer", KEY),
                        lambda: st.queue_pop_head("reviewer", KEY, HEAD),
                        lambda: st.queue_pop_if("reviewer", KEY, {"head": HEAD}),
                        lambda: st.queue_drop_unreadable("reviewer"), lambda: st.queue_all()],
        }
        for name, fns in calls.items():
            path = getattr(st, "locks" if name == "locks" else "pending")
            for junk in JUNK:
                for seat_level in (True, False):
                    for fn in fns:
                        with self.subTest(file=name, junk=junk, seat_level=seat_level):
                            self.plant(path, junk, seat_level=seat_level)
                            fn()          # must not raise
        # A write after junk leaves a well-formed claim behind.
        self.plant(st.locks, "junk-string")
        st.acquire("reviewer", KEY, HEAD, "why", run="r1")
        self.assertIn(KEY, st.live_locks("reviewer"))
        self.plant(st.pending, ["junk"])
        st.queue_add("reviewer", KEY, HEAD, "u", "busy")
        self.assertIn(KEY, st.queue_items("reviewer"))

    def test_inflight_marks_survive_every_shape(self):
        st, mark = self.st, f"review:7:{HEAD}"
        for junk in ["junk", ["x"], 3, None, {mark: "never"}, {mark: True}, {mark: None},
                     {mark: ["x"]}, {"other": "never"}]:
            with self.subTest(junk=junk):
                st.inflight_file.write_text(json.dumps(junk))
                self.assertFalse(st.inflight(mark))          # unageable reads as absent
                self.assertEqual(st.inflight_at(mark), 0.0)
                st.inflight(mark, record=True)               # the prune drops what it can't age
                self.assertTrue(st.inflight(mark))
                st.inflight_clear(mark)
                st.quarantine(7, HEAD)

    def test_record_keeps_marks_within_a_ttl_longer_than_a_day(self):
        """#736: the arming prune must not discard a mark the read path still honors."""
        st = self.st
        st.loop["inflight_ttl_min"] = 3 * 24 * 60
        old = f"review:7:{HEAD}"
        st.inflight_file.write_text(json.dumps({old: time.time() - 30 * 3600}))
        self.assertTrue(st.inflight(old))
        st.inflight(f"review:8:{HEAD}", record=True)          # arming another head prunes
        self.assertTrue(st.inflight(old))                    # the 30h-old mark survives
        st.loop["inflight_ttl_min"] = 10                     # default TTL: still pruned at 24h
        st.inflight_file.write_text(json.dumps({old: time.time() - 30 * 3600}))
        st.inflight(f"review:8:{HEAD}", record=True)
        self.assertNotIn(old, json.loads(st.inflight_file.read_text()))

    def test_breach_markers_survive_every_shape(self):
        st = self.st
        entry = {"pr": 7, "head": HEAD, "rounds": 3, "cap": 3, "reason": "cap", "at": "now"}
        pending = {**entry, "status": "delivery-pending", "delivery_token": "t",
                   "delivery_at": "never"}
        for junk in ["junk", ["x"], 3, None, {KEY: "junk"}, {KEY: ["x"]}, {KEY: pending}]:
            with self.subTest(junk=junk):
                st.breach.write_text(json.dumps(junk))
                st.breach_get(7)
                st.breach_all()
                st.breach_claim(7, HEAD)
                st.breach_start(7, HEAD, 3)
                st.breach_resume(7, HEAD, 3)
                st.quarantine(7, HEAD)
                st.breach.write_text(json.dumps(junk))
                st.breach_set(7, dict(entry))
                st.breach.write_text(json.dumps(junk))
                # The writer that finalizes a delivery (#573 review F1): a junk marker found
                # after sending must be healed, never indexed.
                st.breach_deliver(7, dict(entry), lambda: True,
                                  lambda marker: (st.breach.write_text(json.dumps(junk)), True)[1])


class QuarantineAtomic(unittest.TestCase):
    """#735: quarantine() clears the in-flight marks and the breach marker as one step."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        loop = {"id": "q", "repo": "acme/widgets", "base": "main", "cap": 3,
                "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"], "reviewers": ["r"],
                "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)
        self.st.inflight(f"review:7:{HEAD}", record=True)
        self.st.breach.write_text(json.dumps(
            {KEY: {"pr": 7, "head": HEAD, "status": "awaiting-adjudication"}}))

    def test_state_lock_is_held_while_the_breach_marker_is_cleared(self):
        st = self.st
        real = st._breach_lock
        probes = []

        def probe():
            fd = os.open(st.dir / "state.lock", os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                probes.append("free")
            except BlockingIOError:
                probes.append("held")
            finally:
                os.close(fd)
            return real()

        with mock.patch.object(st, "_breach_lock", probe):
            st.quarantine(7, HEAD)
        self.assertEqual(probes, ["held"])
        self.assertEqual(st.breach_get(7), {})
        self.assertFalse(st.inflight(f"review:7:{HEAD}"))

    def test_crash_between_the_two_writes_leaves_no_claimable_marker(self):
        st = self.st
        with mock.patch.object(st, "_save_strict", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                st.quarantine(7, HEAD)
        self.assertEqual(st.breach_get(7), {})


class ExplainKinds(unittest.TestCase):
    def test_every_kind_explain_assigns_is_declared(self):
        # #546: a branch must not invent a conclusion nobody checks for.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(gate.explain))
        used = {node.value.value for node in ast.walk(tree)
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and any(isinstance(t, ast.Name) and t.id == "kind" for t in node.targets)}
        self.assertTrue(used, "no kind assignments found: the scan is broken")
        self.assertEqual(sorted(used - set(gate.EXPLAIN_KINDS)), [])


class WatchdogLog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        loop = {"id": "log", "repo": "acme/widgets", "base": "main", "cap": 3,
                "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"], "reviewers": ["r"],
                "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(loop)

    def test_watchdog_log_is_bounded(self):
        # #549: note() prunes watchdog.log to its last LOG_KEEP lines once it is large.
        total = state.LOG_KEEP * 2
        for i in range(total):
            self.st.note(f"sweep {i} " + "x" * 300)
        lines = self.st.log.read_text().splitlines()
        self.assertLess(len(lines), total)
        self.assertIn(f"sweep {total - 1} ", lines[-1])
        self.assertLessEqual(len(lines), state.LOG_KEEP + 1)

    def test_oversized_lines_are_bounded(self):
        # #604: a few huge lines (under LOG_KEEP) must not leave the log above its bound.
        self.st.note("x" * 300000)
        self.assertLessEqual(self.st.log.stat().st_size, state.LOG_PRUNE_BYTES)
        for _ in range(100):
            self.st.note("y" * state.LOG_LINE_MAX)
        self.assertLessEqual(self.st.log.stat().st_size,
                             state.LOG_PRUNE_BYTES + state.LOG_LINE_MAX + 100)

    def test_note_during_prune_is_not_lost(self):
        # #732: a line another writer appends while a prune is between its read and its
        # replace must survive. The late writer runs in a thread, started right after the
        # pruner's read of the log.
        self.st.log.parent.mkdir(parents=True, exist_ok=True)
        self.st.log.write_text("".join(f"old {i} " + "x" * 300 + "\n" for i in range(1000)))
        self.assertGreater(self.st.log.stat().st_size, state.LOG_PRUNE_BYTES)
        real, late = Path.read_text, []

        def racing_read(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if path == self.st.log and not late:
                t = threading.Thread(target=self.st.note, args=("late writer",))
                late.append(t)
                t.start()
                t.join(0.5)  # unlocked: the append lands after the read; locked: it waits
            return text

        with mock.patch.object(Path, "read_text", racing_read):
            self.st.note("pruner")
        late[0].join(10)
        text = self.st.log.read_text()
        self.assertIn("pruner", text)
        self.assertIn("late writer", text)
        self.assertLess(len(text.splitlines()), 1000)  # the prune did run

    def test_small_watchdog_log_is_kept_whole(self):
        for i in range(5):
            self.st.note(f"m{i}")
        self.assertEqual(len(self.st.log.read_text().splitlines()), 5)


class Durability(unittest.TestCase):
    """#733: every state write is temp file, fsync, replace, then an fsync of the directory."""

    setUp = Junk.setUp  # the same throwaway loop state; none of Junk's tests

    def fsyncs_during(self, action):
        """Run ``action``; name what was fsynced, as ``file`` or ``dir``."""
        real, seen = os.fsync, []

        def spy(fd):
            seen.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real(fd)

        with mock.patch.object(state.os, "fsync", spy):
            action()
        return seen

    def test_queue_write_fsyncs_file_then_directory(self):
        seen = self.fsyncs_during(
            lambda: self.st.queue_add("reviewer", KEY, HEAD, "u", "busy"))
        self.assertEqual(seen, ["file", "dir"])
        self.assertIn(KEY, json.loads(self.st.pending.read_text())["reviewer"])

    def test_log_prune_fsyncs_file_then_directory(self):
        self.st.log.write_text("old line\n" * (state.LOG_PRUNE_BYTES // 9 + 10))
        seen = self.fsyncs_during(lambda: self.st.note("fresh"))
        self.assertEqual(seen, ["file", "dir"])
        lines = self.st.log.read_text().splitlines()
        self.assertTrue(lines[-1].endswith("fresh"))
        self.assertLessEqual(len(lines), state.LOG_KEEP)
        self.assertEqual([p.name for p in self.st.dir.iterdir() if p.name.endswith(".tmp")], [])


class ExclusiveAcquire(unittest.TestCase):
    """#727: acquisition has a compare-and-swap form, and the gate's claim uses it."""

    setUp = Junk.setUp

    def test_exclusive_acquire_never_evicts_a_live_claim(self):
        st = self.st
        self.assertTrue(st.acquire("reviewer", KEY, HEAD, "first", run="r1", exclusive=True))
        first = st.live_locks("reviewer")[KEY]
        # A second check-then-act caller (another run, or the gate) loses and writes nothing.
        self.assertFalse(st.acquire("reviewer", KEY, HEAD, "second", run="r2", exclusive=True))
        self.assertFalse(st.acquire("reviewer", KEY, HEAD, "gate", exclusive=True))
        self.assertEqual(st.live_locks("reviewer")[KEY], first)
        # Default stays last-writer-wins for callers that mean to replace.
        self.assertTrue(st.acquire("reviewer", KEY, HEAD, "forced", run="r3"))
        self.assertEqual(st.live_locks("reviewer")[KEY]["run"], "r3")

    def test_exclusive_acquire_replaces_expired_and_stamps_the_gates_claim(self):
        st = self.st
        st.acquire("reviewer", KEY, HEAD, "old")
        self.assertTrue(st.acquire("reviewer", KEY, HEAD, "run stamp", run="r1", exclusive=True))
        self.assertEqual(st.live_locks("reviewer")[KEY]["run"], "r1")
        data = json.loads(st.locks.read_text())
        data["reviewer"][KEY]["at"] = 1.0                      # long expired
        st.locks.write_text(json.dumps(data))
        self.assertTrue(st.acquire("reviewer", KEY, HEAD, "again", run="r2", exclusive=True))
        self.assertEqual(st.live_locks("reviewer")[KEY]["run"], "r2")

    def test_a_claim_dated_ahead_of_the_clock_is_expired_not_live_forever(self):
        """#794: a negative age used to sit inside every TTL, holding the seat unseen."""
        st = self.st
        for ahead in (60.0, 10 * 365 * 86400.0, time.time() * 1000):   # incl. a ms timestamp
            with self.subTest(ahead=ahead):
                st.acquire("reviewer", KEY, HEAD, "old", run="r1")
                data = json.loads(st.locks.read_text())
                data["reviewer"][KEY]["at"] = time.time() + ahead
                st.locks.write_text(json.dumps(data))
                self.assertEqual(st.live_locks("reviewer"), {})
                self.assertEqual(st.active("reviewer"), {})          # and the ledger is healed
                self.assertNotIn(KEY, json.loads(st.locks.read_text()).get("reviewer", {}))
                # the seat is free again for a fresh claim
                self.assertTrue(st.acquire("reviewer", KEY, HEAD, "new", run="r2", exclusive=True))
                st.release_all("reviewer")

    def test_take_seat_does_not_evict_a_claim_made_while_it_decided(self):
        from unittest import mock
        st = self.st
        loop = st.loop
        # The race: the gate's checks all passed, then another process claimed the PR.
        real = st.acquire

        def rival_first(*a, **kw):
            real("reviewer", gate.seat_key(loop, 7), HEAD, "rival", run="rival")
            return real(*a, **kw)
        with mock.patch.object(st, "acquire", side_effect=rival_first), \
                mock.patch.object(gate.isolation, "ensure", return_value=None) as ensure:
            with self.assertRaises(SystemExit):
                gate.take_seat(loop, st, "reviewer", 7, HEAD, "mine")
        ensure.assert_not_called()                               # no second workspace/run
        self.assertEqual(st.live_locks("reviewer")[gate.seat_key(loop, 7)]["run"], "rival")


if __name__ == "__main__":
    unittest.main()
