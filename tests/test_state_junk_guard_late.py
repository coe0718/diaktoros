"""#2589 review (round 10): the guard on the walk (``tests/_state_walk.py``) when a valid value
turns up late, beside ``tests/test_state_junk_guard_deep.py``.

A writer that writes its value only once another file holds data (``gz_on``: the trigger's
``ready: true``, only after ``gc_enable``) adds that value after the reader (``gb_read``) was
traced, and leaves the folded sample as it was (``ga_off``'s ``ready: false`` came first). The
reader is traced again with it, so the target it opens is found and fuzzed. A method whose state
never stops changing (``hh_grow``) fails the walk rather than ending discovery half done.
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

    def test_state_that_never_settles_fails_closed(self):
        failures = self.walk.run(["hh_grow"])
        self.assertIn(f"hh_grow (discovery): still reaching new state after {walk.ROUNDS} rounds, "
                      "so its files may not all be found", failures)


if __name__ == "__main__":
    unittest.main()
