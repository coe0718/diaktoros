"""#2589 review (round 3): the guard on the walk (``tests/_state_walk.py``) past one file's top
level, beside ``tests/test_state_junk_guard.py``.

F1: a file whose name is built at run time and read only when ANOTHER state file holds data
(Vex's probe: ``trigger.json`` non-empty, then ``hidden-state.json``) is found and planted, so
junk in it fails the walk: found on a dir seeded with what the methods write, or, when no method
writes the trigger, planted on a further pass once a junk call touched it, else a failure.

F2: a reader that hands back the stored file (``fix_handoff`` returning the raw ``fix-asks.json``
object) fails on an object of junk, which reads as a missing file whatever its top level; and a
writer that rewrites an object but keeps the junk inside what it writes fails too.

F8 (round 7): junk in a file read only when ANOTHER file holds a valid value is reached, though
every all-junk call replaces that value with junk: each file is also fuzzed alone, beside clean
companions (Vex's probe: ``arm_probe`` writes the trigger, ``read_probe`` then reads the target).
And (round 8) whichever valid value the companion must hold: two writers' conflicting values
(``aa_off`` writes ``ready: false``, ``bb_on`` ``ready: true``) are each laid beside the target.
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

PROBES = '''
import json
import time


class ProbeMixin:
    def trigger_arm(self, number: int) -> None:
        self._save(self.dir / "trigger.json", {str(number): True})

    def hidden_state(self) -> dict:
        if not self._load(self.dir / "trigger.json", {}):
            return {}
        path = self.dir / ("hidden-" + "state" + ".json")      # spelled nowhere whole
        data = json.loads(path.read_text()) if path.exists() else {}
        return {**data}                                       # raises on a non-dict

    def lone_hidden(self) -> dict:                            # no method writes lone-trigger.json
        if not self._load(self.dir / "lone-trigger.json", {}):
            return {}
        path = self.dir / ("lone-" + "hidden" + ".json")
        data = json.loads(path.read_text()) if path.exists() else {}
        return {**data}

    def merge_fields(self, number: int) -> None:              # keeps a junk at, head or since
        path = self.dir / "merge-fields.json"
        data = self._load(path, {})
        data = data if isinstance(data, dict) else {}
        entry = data.get(str(number))
        entry = entry if isinstance(entry, dict) else {}
        entry.setdefault("at", time.time())
        entry.setdefault("head", "h")
        entry.setdefault("since", "2026-01-01T00:00:00Z")
        data[str(number)] = entry
        self._save(path, data)

    def arm_probe(self) -> None:
        self._save(self.dir / "independent-trigger.json", {"ready": True})

    def read_probe(self) -> dict:
        trigger = self._load(self.dir / "independent-trigger.json", {})
        if not (isinstance(trigger, dict) and trigger.get("ready") is True):
            return {}
        path = self.dir / "independent-target.json"
        return {**json.loads(path.read_text())} if path.exists() else {}

    def aa_off(self) -> None:                                 # first: the folded sample's
        self._save(self.dir / "conflict-trigger.json", {"ready": False})

    def bb_on(self) -> None:
        self._save(self.dir / "conflict-trigger.json", {"ready": True})

    def cc_read(self) -> dict:
        trigger = self._load(self.dir / "conflict-trigger.json", {})
        if not (isinstance(trigger, dict) and trigger.get("ready") is True):
            return {}
        path = self.dir / ("conflict-" + "target" + ".json")
        return {**json.loads(path.read_text())} if path.exists() else {}

    def merge_entry(self, number: int) -> None:               # keeps a junk entry whole
        path = self.dir / "merge-entry.json"
        data = self._load(path, {})
        data = data if isinstance(data, dict) else {}
        data.setdefault(str(number), {"at": time.time(), "head": "h"})
        self._save(path, data)
'''
RAISING = sum(bool(s) and not isinstance(s, dict) for s in walk.SHAPES)  # opens, then {**junk}
BESIDE = sum(not isinstance(s, dict) for s in walk.SHAPES)    # a clean trigger: None raises too


def of(failures: list[str], method: str, text: str = "", everywhere=False) -> list[str]:
    """``method``'s failures holding ``text`` (only the all-junk pass's when ``everywhere``)."""
    return [f for f in failures if f.startswith(f"{method}[") and text in f
            and not (everywhere and ".json]: " in f)]


class TheWalkCatchesDeeper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """``LoopState`` with the probes' mixin, and a walk of it and of ``LoopState`` (each
        discovered once, then reused)."""
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        source = cls.root / "probe_mixin.py"
        source.write_text(PROBES)
        spec = importlib.util.spec_from_file_location("probe_mixin", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.cls = type("ProbeLoopState", (module.ProbeMixin, state.LoopState),
                       {"__module__": module.__name__})
        cls.walk = walk.Walk(cls.cls, cls.root / "probe")
        cls.real_walk = walk.Walk(state.LoopState, cls.root / "real")
        cls.walk.discover()                       # now, before any test patches LoopState
        cls.real_walk.discover()

    def test_a_file_read_only_when_another_holds_data_is_found(self):
        st = self.cls(walk.new_loop(self.root / "empty"))
        st.dir.mkdir(parents=True)
        with walk.touches(st.dir) as seen:                    # what an empty dir shows
            st.hidden_state()
        self.assertEqual(seen, {"trigger.json"})
        self.assertNotIn("hidden-state.json", walk.spelled_json(self.cls))
        found = self.walk.discover()
        self.assertIn("hidden-state.json", found)             # on the seeded dir
        self.assertNotIn("lone-hidden.json", found)           # nothing writes its trigger

    def test_junk_in_a_file_read_only_when_another_holds_data_fails_the_walk(self):
        failures = self.walk.run(["hidden_state"])
        raised = of(failures, "hidden_state", "raised TypeError", everywhere=True)
        self.assertEqual(len(raised), RAISING, failures)
        self.assertEqual(len(of(failures, "hidden_state", "state.json]: raised TypeError")),
                         BESIDE, failures)

    def test_a_file_only_junk_opens_is_planted_on_a_further_pass(self):
        failures = self.walk.run(["lone_hidden"])
        self.assertEqual(len(of(failures, "lone_hidden", "raised TypeError", everywhere=True)),
                         RAISING, failures)
        self.assertEqual(self.walk.strays, {"lone-hidden.json"})

    def test_a_file_touched_on_junk_and_never_planted_fails_closed(self):
        with mock.patch.object(walk, "ROUNDS", 1):
            failures = self.walk.run(["lone_hidden"])
        self.assertIn("lone-hidden.json: touched on junk, and still not planted after 1 passes, "
                      "so never fuzzed", failures)

    def test_junk_read_only_beside_a_valid_companion_fails_the_walk(self):
        self.assertIn("independent-target.json", self.walk.discover())
        failures = self.walk.run(["read_probe"])
        beside = of(failures, "read_probe", "in independent-target.json]: raised TypeError")
        self.assertEqual(len(beside), BESIDE, failures)
        self.assertEqual(of(failures, "read_probe", everywhere=True), [])  # all-junk: unreached

    def test_junk_read_only_beside_a_conflicting_writers_value_fails_the_walk(self):
        self.assertIn("conflict-target.json", self.walk.discover())   # on bb_on's value
        self.assertEqual(self.walk.samples["conflict-trigger.json"], {"ready": False})
        failures = self.walk.run(["cc_read"])
        beside = of(failures, "cc_read", "in conflict-target.json]: raised TypeError")
        self.assertEqual(len(beside), BESIDE, failures)

    def test_a_reader_returning_the_stored_object_fails_the_walk(self):
        # Vex's stub: fix_handoff returns fix-asks.json itself when it is an object. Right on a
        # missing file and on every scalar; wrong on an object of junk, which holds no hand-off.
        def raw(self, number: int) -> "dict | None":
            data = self._load(self.dir / "fix-asks.json", None)
            return data if isinstance(data, dict) else None
        self.assertEqual(self.real_walk.run(["fix_handoff"]), [])
        with mock.patch.object(state.LoopState, "fix_handoff", raw):
            failures = self.real_walk.run(["fix_handoff"])
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("read junk as {'7': 'junk'", failures[0])
        self.assertIn("not as a missing file (None)", failures[0])

    def test_a_writer_keeping_junk_inside_what_it_writes_fails_the_walk(self):
        failures = self.walk.run(["merge_fields", "merge_entry"])
        for text in ("merge-fields.json, which holds a str at /7/at, where a clean call writes "
                     "a number", "merge-fields.json, which holds a number at /7/head",
                     "merge-fields.json, which kept junk 'never' at /7/since",
                     "merge-entry.json, which holds a str at /7, where a clean call writes a dict",
                     "merge-entry.json, which lacks /7/head"):
            self.assertTrue(any(text in f for f in failures), (text, failures))
        self.assertTrue(all("rewrote merge-" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
