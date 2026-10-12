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
import unittest

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

    def test_small_watchdog_log_is_kept_whole(self):
        for i in range(5):
            self.st.note(f"m{i}")
        self.assertEqual(len(self.st.log.read_text().splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
