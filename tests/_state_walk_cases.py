"""The cases ``tests/_state_walk.py`` walks (#2303): the junk shapes, the argument each
parameter name gets, and the registries of methods walked differently, each with its reason."""
from __future__ import annotations

from pathlib import Path

HEAD = "a" * 40
KEY = "acme/widgets#7"
NEVER = "never"
SCALARS = [None, "junk", 7, True]        # a top level no state file holds: read as missing
LIST = ["a", 1]
FLAT = {"7": "junk", "8": ["x"], "reviewer": "junk", KEY: "junk", "open": "junk", "count": "junk",
        "approved": "junk", "handoff": "junk", "last_failure": "junk", "entries": "junk"}
# The shapes that hold no valid entry (no object at any key): read exactly as a missing file.
EMPTY = [*SCALARS, LIST, FLAT]
SHAPES = [
    *EMPTY,
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
# Strings the shapes plant that no valid state holds: a file a call rewrites keeps none of them
# where the call writes (unless its own clean write holds one too, such as an argument "x").
JUNK_STRINGS = frozenset({"junk", "x", NEVER, "Infinity"})
ARGS = {
    "seat": "reviewer", "key": KEY, "number": 7, "issue": 7, "pr": 8, "prerequisite": 8,
    "head": HEAD, "base_sha": HEAD, "entry": {"head": HEAD}, "data": {}, "default": {},
    "fields": {}, "facts": {}, "new": {}, "written": {},
    "hold": {"by": "boss", "at": "2026-01-01T00:00:00Z"},
    "record": {"by": "boss", "at": "2026-01-01T00:00:00Z"}, "expected": None, "previous": None,
    "at": 1.0, "rounds": 1, "review_id": 1, "comment_id": 1, "numbers": [7], "approve": False,
    "update": False, "outcome": "x", "message": "x", "identity": "r", "kind": "plan",
    "text": "x", "run": "r1", "reply": "x", "by": "boss", "url": "u", "reason": "x",
    "why": "x", "tried": "x", "current": lambda: True, "send": lambda entry: True,
    "check": lambda *args, **kwargs: True,
}
# Where a parameter name means something else for one method.
METHOD_ARGS = {"minutes": {"key": "inflight_ttl_min"}}
# Public methods not called, with why.
SKIP: dict[str, str] = {}
# Methods whose result is the file's JSON as is, so no return shape is checked, and why.
ANY_SHAPE = {
    "kv_get": "the key-value API (#1088) returns the JSON at a caller's own path, or the "
              "default when unreadable; each caller checks the shape it keeps there",
}
# Each EMPTY shape reads as a missing file: the method returns what it returns with no file. The
# exceptions, for a method whose result keeps a valid part of such a shape: the shapes, and why.
KEEPS = {
    "dep_waits": ([LIST], "1 in ['a', 1] is a valid issue number: the junk 'a' is dropped, the "
                          "wait on issue 1 kept"),
}
# Methods with no return annotation, and the return type the walk holds them to.
HINTS = {"review_only_conflict": tuple[dict, str, bool] | None}
LOCK = object()                                  # what ``call`` returns for a lock it took
# Methods that refuse junk on purpose: the one exception each may raise, and why.
RAISES = {
    "issue_dep_add": (ValueError, "an unreadable dependency record is never overwritten "
                                  "(#2399 review F1); the caller reports it"),
    "issue_dep_drop": (ValueError, "both dependency records are read strictly first; an "
                                   "unreadable one removes nothing (#2399)"),
    "fix_ask_confirm": (ValueError, "confirms run's own pending claim only: with no such claim "
                                    "(junk or none) it raises, as documented"),
}


def new_loop(root: Path) -> dict:
    return {"id": "junk", "repo": "acme/widgets", "base": "main", "cap": 3,
            "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
            "state_dir": str(root / "state"), "fixers": ["f"], "reviewers": ["r"],
            "seats": {"reviewer": {}, "fixer": {}}}
