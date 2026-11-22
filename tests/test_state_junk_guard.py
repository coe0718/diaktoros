"""#2303 review: the guard on ``tests/test_state_junk_all.py``'s walk (``tests/_state_walk.py``).

F1: a new public method, in a new mixin module, reading a new ``*.json`` file and raising on a
non-dict, is planted with junk and caught, with no edit to the test: once through a name its
module spells, once through a name built at run time (found by watching the call).

F2: a reader that returns junk (``fix_handoff`` returning a string), or that declares no return
type, or a writer that rewrites junk into another shape instead of healing it, fails the walk.
The real class passes it (``test_state_junk_all``).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import state  # noqa: E402
import _state_walk as walk  # noqa: E402

FUTURE = '''
import json
import os


class FutureMixin:
    def future_state(self) -> dict:
        path = self.dir / "future-state.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise TypeError("future-state.json is not an object")
        return data

    def future_built(self) -> dict:
        path = self.dir / ("future-" + "built" + ".json")     # spelled nowhere whole
        data = json.loads(path.read_text()) if os.path.exists(path) else {}
        return {**data}                                    # raises on a non-dict

    def future_wrap(self) -> None:
        path = self.dir / "future-log.json"
        raw = self._load(path, {})
        self._save(path, {**raw, "n": 1} if isinstance(raw, dict) else [raw, "n"])
'''


def failures_of(failures: list[str], method: str) -> list[str]:
    return [f for f in failures if f.startswith(f"{method}[")]


class TheWalkCatches(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def future_class(self):
        """``LoopState`` with a mixin from a module of its own, as a new feature would add one."""
        source = self.root / "future_mixin.py"
        source.write_text(FUTURE)
        spec = importlib.util.spec_from_file_location("future_mixin", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return type("FutureLoopState", (module.FutureMixin, state.LoopState),
                    {"__module__": module.__name__})

    def test_a_new_mixins_files_are_found_and_planted(self):
        future = self.future_class()
        self.assertIn("future_state", walk.public_methods(future))
        found = walk.Walk(future, self.root / "find").discover()
        for name in ("future-state.json", "future-built.json", "future-log.json"):
            self.assertIn(name, found)
        self.assertIn("future-state.json", walk.spelled_json(future))       # by its module
        self.assertNotIn("future-built.json", walk.spelled_json(future))    # only by watching
        real = walk.Walk(state.LoopState, self.root / "real").discover()
        self.assertNotIn("future-state.json", real)

    def test_a_new_method_raising_on_its_own_junk_file_fails_the_walk(self):
        failures = walk.Walk(self.future_class(), self.root / "walk").run(
            ["future_state", "future_built"])
        for method in ("future_state", "future_built"):
            raised = failures_of(failures, method)
            self.assertTrue(raised and all("raised TypeError" in f for f in raised), failures)
            self.assertEqual(len(raised), sum(not isinstance(s, dict) for s in walk.SHAPES))

    def test_a_writer_that_wraps_junk_instead_of_healing_fails_the_walk(self):
        failures = walk.Walk(self.future_class(), self.root / "walk").run(["future_wrap"])
        self.assertTrue(failures, "a junk file rewritten as a list passed")
        self.assertTrue(all("rewrote future-log.json as a list, not healed" in f
                            for f in failures), failures)

    def test_a_reader_returning_junk_fails_the_walk(self):
        # The review's mutation (F2): fix_handoff returns a string. With its declared type kept
        # (a broken body under the same def line) it is caught as the wrong type, and on a junk
        # top level as not reading junk as a missing file.
        def unhealed(self, number: int) -> "dict | None":
            return "unhealed junk"
        with mock.patch.object(state.LoopState, "fix_handoff", unhealed):
            failures = walk.Walk(state.LoopState, self.root / "a").run(["fix_handoff"])
        self.assertEqual(len(failures), len(walk.SHAPES), failures)
        self.assertTrue(all("returned 'unhealed junk', not dict | None" in f
                            for f in failures), failures)

    def test_a_reader_that_leaks_junk_only_on_junk_fails_the_walk(self):
        # Right on a missing file, wrong on junk: a reader that passes the file's value through.
        def leaky(self, number: int) -> "dict | None":
            raw = self._load(self.dir / "fix-asks.json", None)
            return raw if raw is not None and not isinstance(raw, dict) else None
        with mock.patch.object(state.LoopState, "fix_handoff", leaky):
            failures = walk.Walk(state.LoopState, self.root / "b").run(["fix_handoff"])
        self.assertTrue(any("read junk as 'junk', not as a missing file (None)" in f
                            for f in failures), failures)
        self.assertTrue(any("returned ['a', 1], not dict | None" in f for f in failures),
                        failures)

    def test_a_method_declaring_no_return_type_fails_the_walk(self):
        # The review's literal stub: no annotation, so its result could not be checked at all.
        with mock.patch.object(state.LoopState, "fix_handoff",
                               lambda self, number: "unhealed junk"):
            failures = walk.Walk(state.LoopState, self.root / "c").run(["fix_handoff"])
        self.assertEqual(len(failures), len(walk.SHAPES), failures)
        self.assertTrue(all("declares no return type" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
