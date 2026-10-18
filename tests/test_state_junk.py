"""#545, #547, #548: the state files' writers tolerate junk the way their readers do (#80).

A hand edit, a torn write or an older writer can leave a value of the wrong shape in
``locks.json``, ``pending.json``, ``inflight.json`` or ``breach.json``. The readers already
treated such a value as absent; the writers raised on it, so one bad entry crashed every gate
run (and the watchdog re-drove it into the same crash). Every reader and writer here is run
against every junk shape: none may raise, and a write leaves a well-formed file behind.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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
