"""#2589 review (round 10): the guard on the walk (``tests/_state_walk.py``) when a valid value
turns up late, beside ``tests/test_state_junk_guard_deep.py``.

A writer that writes its value only once another file holds data (``gz_on``: the trigger's
``ready: true``, only after ``gc_enable``) adds that value after the reader (``gb_read``) was
traced, and leaves the folded sample as it was (``ga_off``'s ``ready: false`` came first). The
reader is traced again with it, so the target it opens is found and fuzzed. A method whose state
never stops changing (``hh_grow``) fails the walk rather than ending discovery half done.

Round 11: two writers' counts (``ia_two``'s ``count: 2``, ``ib_three``'s ``count: 3``) are two
alternatives, so a reader that opens a file only on one of them (``ic_read``) reaches it; a
writer that adds one each call (``id_bump``) still lets discovery settle.

Round 12: one writer's count that changes only with another file (``ie_gated``: 2 alone, 3 once
``gc_enable`` ran) is kept too, so the reader that opens a file only on 3 (``if_read``) reaches it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import state  # noqa: E402
import _state_walk as walk  # noqa: E402

PROBES = '''
import json


class LateMixin:
    def ga_off(self) -> None:
        self._save(self.dir / "late-trigger.json", {"ready": False})

    def gb_read(self) -> dict:
        trigger = self._load(self.dir / "late-trigger.json", {})
        if not (isinstance(trigger, dict) and trigger.get("ready") is True):
            return {}
        path = self.dir / ("late-" + "target" + ".json")      # spelled nowhere whole
        return {**json.loads(path.read_text())} if path.exists() else {}

    def gc_enable(self) -> None:
        self._save(self.dir / "late-enable.json", {"on": True})

    def gz_on(self) -> None:
        enable = self._load(self.dir / "late-enable.json", {})
        if isinstance(enable, dict) and enable.get("on") is True:
            self._save(self.dir / "late-trigger.json", {"ready": True})

    def ia_two(self) -> None:
        self._save(self.dir / "late-count.json", {"count": 2})

    def ib_three(self) -> None:
        self._save(self.dir / "late-count.json", {"count": 3})

    def ic_read(self) -> dict:
        count = self._load(self.dir / "late-count.json", {})
        if not (isinstance(count, dict) and count.get("count") == 3):
            return {}
        path = self.dir / ("late-" + "counted" + ".json")     # spelled nowhere whole
        return {**json.loads(path.read_text())} if path.exists() else {}

    def id_bump(self) -> None:                                # adds one each call
        data = self._load(self.dir / "late-tally.json", {})
        seen = data.get("count") if isinstance(data, dict) else 0
        self._save(self.dir / "late-tally.json", {"count": (seen if type(seen) is int else 0) + 1})

    def ie_gated(self) -> None:                               # 3 only beside the enable file
        enable = self._load(self.dir / "late-enable.json", {})
        on = isinstance(enable, dict) and enable.get("on") is True
        self._save(self.dir / "late-gated.json", {"count": 3 if on else 2})

    def if_read(self) -> dict:
        count = self._load(self.dir / "late-gated.json", {})
        if not (isinstance(count, dict) and count.get("count") == 3):
            return {}
        path = self.dir / ("late-" + "opened" + ".json")      # spelled nowhere whole
        return {**json.loads(path.read_text())} if path.exists() else {}

    def hh_grow(self) -> None:                                # never the same twice
        data = self._load(self.dir / "late-grow.json", [])
        self._save(self.dir / "late-grow.json", [*(data if isinstance(data, list) else []), "x"])
'''
BESIDE = sum(not isinstance(s, dict) for s in walk.SHAPES)


class TheWalkCatchesLateValues(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "late_mixin.py"
        source.write_text(PROBES)
        spec = importlib.util.spec_from_file_location("late_mixin", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.cls = type("LateLoopState", (module.LateMixin, state.LoopState),
                       {"__module__": module.__name__})
        cls.walk = walk.Walk(cls.cls, root / "late")
        cls.walk.discover()

    def test_a_value_written_after_its_reader_was_traced_is_laid_beside_it(self):
        self.assertIn("late-target.json", self.walk.discover())
        self.assertEqual(self.walk.samples["late-trigger.json"], {"ready": False})
        failures = self.walk.run(["gb_read"])
        beside = [f for f in failures if "in late-target.json]: raised TypeError" in f]
        self.assertEqual(len(beside), BESIDE, failures)

    def test_two_writers_counts_are_both_laid_beside_a_reader(self):
        self.assertIn("late-counted.json", self.walk.discover())
        failures = self.walk.run(["ic_read"])
        beside = [f for f in failures if "in late-counted.json]: raised TypeError" in f]
        self.assertEqual(len(beside), BESIDE, failures)

    def test_one_writers_count_beside_another_file_is_laid_beside_a_reader(self):
        self.assertIn("late-opened.json", self.walk.discover())
        failures = self.walk.run(["if_read"])
        beside = [f for f in failures if "in late-opened.json]: raised TypeError" in f]
        self.assertEqual(len(beside), BESIDE, failures)

    def test_one_writers_growing_count_still_settles(self):
        self.assertNotIn("id_bump (discovery)", " ".join(self.walk.uncovered))

    def test_state_that_never_settles_fails_closed(self):
        failures = self.walk.run(["hh_grow"])
        self.assertIn(f"hh_grow (discovery): still reaching new state after {walk.ROUNDS} rounds, "
                      "so its files may not all be found", failures)


if __name__ == "__main__":
    unittest.main()
