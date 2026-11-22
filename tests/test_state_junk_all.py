"""#2303: every public ``LoopState`` method survives junk in every JSON state file (#80).

#545, #547, #548, #566 and #567 were each a state file whose reader or writer raised on a value
of the wrong shape. ``test_state_junk.py`` fuzzes a hand-picked set of methods; this one finds
the methods by introspection and the files by scanning every module the class is built from and
by watching what each method touches on an empty state dir, so a new method, in a new mixin,
reading a new file, is fuzzed without editing the test (``tests/_state_walk.py`` has the walk;
``tests/test_state_junk_guard.py`` proves it catches such a method, and a reader that returns
junk). Each junk shape is written to every file at once; each call must not raise, must return
its declared type (a junk top level as a missing file), and must heal any file it rewrites.

``observations.json`` belongs to ``observer``, whose ledger reader refuses a malformed ledger on
purpose (an unreadable ledger must not erase claims); its entries' ``at`` values (#567) are
fuzzed through ``observer``'s own readers below.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import observer, state  # noqa: E402
import _state_walk as walk  # noqa: E402

NEVER = walk.NEVER


class EveryLoopStateMethod(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.st = state.LoopState(walk.new_loop(self.root))
        self.st.dir.mkdir(parents=True, exist_ok=True)

    def test_the_walk_finds_the_state_files_and_methods(self):
        names = walk.Walk(state.LoopState, self.root / "find").discover()
        for known in ("findings.json", "review-only-caps.json", "locks.json", "pending.json",
                      "fix-asks.json", "observations.json", "issue-deps.json", "kv.json"):
            self.assertIn(known, names)
        methods = walk.public_methods()
        self.assertGreater(len(methods), 80)
        for name in (*walk.SKIP, *walk.RAISES, *walk.METHOD_ARGS, *walk.ANY_SHAPE, *walk.HINTS):
            self.assertIn(name, methods, f"the walk names {name}, which is gone")

    def test_every_public_method_survives_every_shape(self):
        failures = walk.Walk(state.LoopState, self.root / "walk").run()
        self.assertEqual(failures, [], "\n".join(failures[:40]))

    def test_observer_ledger_at_values(self):
        # #567: an entry's ``at`` of the wrong type is skipped, never a crash; a ledger whose
        # top level is not ``{"entries": {...}}`` is refused (ValueError) by design.
        for at in (NEVER, None, [], {"x": 1}, "Infinity", True):
            entries = {"k": {"at": at, "status": "uncertain"}, "j": "junk", "i": {"status": 3}}
            with self.subTest(at=at):
                self.st.observations.write_text(json.dumps({"entries": entries}))
                observer.unsettled(self.st)
                observer.owed(self.st)
                observer.owed_entries(self.st)
                observer.recorded(self.st, "k")
                observer.settle(self.st, "k", True)
        for shape in (None, "junk", [], {"entries": "junk"}):
            self.st.observations.write_text(json.dumps(shape))
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                observer.owed(self.st)


if __name__ == "__main__":
    unittest.main()
