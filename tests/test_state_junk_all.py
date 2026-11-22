"""#2303: every public ``LoopState`` method survives junk in every JSON state file (#80).

#545, #547, #548, #566 and #567 were each a state file whose reader or writer raised on a value
of the wrong shape. ``test_state_junk.py`` fuzzes a hand-picked set of methods; this one finds
the methods by introspection, so a new one is fuzzed without editing the test. The JSON files
are every ``*.json`` name the state modules spell (plus ``observations.json``); each junk shape
in ``SHAPES`` (null, a string, a list, dicts whose values have the wrong type or carry
non-numeric times, ``findings.json``'s non-``F<n>`` keys) is written to all of them at once,
and each method is called with simple arguments chosen by parameter name (``ARGS``). It must
not raise, and any file it rewrote must be well-formed JSON. ``SKIP`` names what is not called,
each with its reason.

``observations.json`` belongs to ``observer``, whose ledger reader refuses a malformed ledger on
purpose (an unreadable ledger must not erase claims); its entries' ``at`` values (#567) are
fuzzed through ``observer``'s own readers below.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import observer, state  # noqa: E402

HEAD = "a" * 40
KEY = "acme/widgets#7"
NEVER = "never"
SHAPES = [
    None, "junk", ["a", 1], 7, True,
    {"7": "junk", "8": ["x"], "reviewer": "junk", KEY: "junk", "open": "junk", "count": "junk",
     "approved": "junk", "handoff": "junk", "last_failure": "junk", "entries": "junk"},
    {"7": {"at": NEVER, "head": 7, "rounds": "x", "findings": {"F": {"state": "open"},
                                                              "Fx": "junk", "F1": "junk"}},
     "reviewer": {KEY: {"at": NEVER, "head": 5, "run": 3}}, KEY: {"at": NEVER},
     "open": {"7": "junk"}, "count": {"7": "x"}, "approved": "x", "handoff": {"7": "x"},
     "last_failure": {"at": NEVER}},
    {"7": {"at": "Infinity", "since": NEVER, "until": [], "state": 3, "8": {"at": NEVER}},
     "reviewer": {KEY: "junk"}, "fixer": [KEY], "last_run": NEVER, "heads": "junk"},
    {"7": {HEAD: {"at": NEVER}, "findings": "junk", "last_head": 7}, "reviewer": [],
     "open": {"7": {"comment_id": "x", "run": 3}}, "count": {"7": None}},
]
ARGS = {
    "seat": "reviewer", "key": KEY, "number": 7, "issue": 7, "pr": 8, "prerequisite": 8,
    "head": HEAD, "base_sha": HEAD, "entry": {}, "data": {}, "default": {}, "fields": {},
    "facts": {}, "new": {}, "written": {}, "hold": {"by": "boss", "at": "2026-01-01T00:00:00Z"},
    "record": {"by": "boss", "at": "2026-01-01T00:00:00Z"}, "expected": None, "previous": None,
    "at": 1.0, "rounds": 1, "review_id": 1, "comment_id": 1, "numbers": [7], "approve": False,
    "update": False, "outcome": "x", "message": "x", "identity": "r", "kind": "plan",
    "text": "x", "run": "r1", "reply": "x", "by": "boss", "url": "u", "reason": "x",
    "why": "x", "tried": "x", "current": lambda: True, "send": lambda entry: True,
    "check": lambda *args, **kwargs: True,
}
ARGS["entry"] = {"head": HEAD}                   # a breach or transition entry names its head
# Where a parameter name means something else for one method.
METHOD_ARGS = {"minutes": {"key": "inflight_ttl_min"}}
# Public methods not called, with why.
SKIP = {}
# Methods that refuse junk on purpose: the one exception each may raise, and why.
RAISES = {
    "issue_dep_add": (ValueError, "an unreadable dependency record is never overwritten "
                                  "(#2399 review F1); the caller reports it"),
    "issue_dep_drop": (ValueError, "both dependency records are read strictly first; an "
                                   "unreadable one removes nothing (#2399)"),
    "fix_ask_confirm": (ValueError, "confirms run's own pending claim only: with no such claim "
                                    "(junk or none) it raises, as documented"),
}


def json_files():
    """Every ``*.json`` file name the state modules spell, and ``observations.json``."""
    names = {state.OBSERVATIONS_FILE, "kv.json"}
    for module in ("__init__.py", "fix_asks.py"):
        tree = ast.parse((ROOT / "diaktoros" / "state" / module).read_text(encoding="utf-8"))
        names |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and n.value.endswith(".json")
                  and "/" not in n.value and " " not in n.value}
    return sorted(names)


def public_methods():
    return sorted(name for name, _ in inspect.getmembers(state.LoopState, inspect.isfunction)
                  if not name.startswith("_"))


class EveryLoopStateMethod(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        loop = {"id": "junk", "repo": "acme/widgets", "base": "main", "cap": 3,
                "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"],
                "reviewers": ["r"], "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)
        self.files = [self.st.dir / name for name in json_files()]

    def plant(self, shape):
        for path in self.files:
            path.write_text(json.dumps(shape))
        return {path: path.read_bytes() for path in self.files}

    def call(self, name):
        method = getattr(self.st, name)
        kwargs = {}
        for param in inspect.signature(method).parameters.values():
            if param.default is not param.empty or param.kind in (param.VAR_POSITIONAL,
                                                                  param.VAR_KEYWORD):
                continue
            own = METHOD_ARGS.get(name, {})
            kwargs[param.name] = (own[param.name] if param.name in own
                                  else self.st.dir / "kv.json" if param.name == "path"
                                  else ARGS[param.name])
        result = method(**kwargs)
        if hasattr(result, "__enter__"):             # a lock: take it and let it go
            with result:
                pass

    def test_the_walk_finds_the_state_files_and_methods(self):
        names = json_files()
        for known in ("findings.json", "review-only-caps.json", "locks.json", "pending.json",
                      "fix-asks.json", "observations.json"):
            self.assertIn(known, names)
        self.assertGreater(len(public_methods()), 80)
        for name in (*SKIP, *RAISES, *METHOD_ARGS):
            self.assertIn(name, public_methods(), f"the test names {name}, which is gone")

    def test_every_public_method_survives_every_shape(self):
        for name in public_methods():
            if name in SKIP:
                continue
            for shape in SHAPES:
                with self.subTest(method=name, shape=shape):
                    before = self.plant(shape)
                    allowed = RAISES[name][0] if name in RAISES else ()
                    try:
                        self.call(name)
                    except allowed:
                        pass
                    except Exception as exc:  # noqa: BLE001 - the failure is the finding
                        self.fail(f"LoopState.{name} raised {type(exc).__name__}: {exc}")
                    for path, was in before.items():
                        now = path.read_bytes() if path.exists() else was
                        if now != was:
                            json.loads(now)          # a rewrite is well-formed

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
