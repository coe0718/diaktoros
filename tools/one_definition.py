"""The one-definition scan that ``tests/test_one_definition.py`` enforces.

Two PRs can each move the same block into a helper of the same name at different places in a
file; git merges both and Python keeps only the later ``def``, so one PR's rule is silently
gone (``explain._queue_blockers`` lost #2075's parked-PR rule that way). ``repeated`` names
each module or class scope that binds one function or class name twice.

A ``def`` decorated with its own name's ``.setter`` or ``.deleter`` builds on the earlier
binding (a property and its setter), so it is not a second definition (#2142); anything else is,
in body order: a plain ``def``/``class`` (``@x.getter`` included, which replaces the property's
getter) after any earlier one of that name, and a setter or deleter after one of its own kind
(#2536 review F2, F4).

It sits here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's scan and not the PR's
(#1973, #2509): with the scan inside the test file, every regression passed on the base too
(#2536 review F1). Its regression tests stay in the test module. Standard library only.
"""
from __future__ import annotations

import ast

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ACCESSORS = {"setter", "deleter"}      # a getter replaces the property's own: a "def"


def _kind(node) -> str:
    """How ``node`` binds its name: ``"setter"`` or ``"deleter"`` for ``@x.setter def x`` and
    ``@x.deleter def x``, which extend the property ``x`` already names rather than replacing it
    (#2142); ``"def"`` for anything else, ``@x.getter`` included, since it replaces the getter
    ``@property def x`` gave."""
    return next((d.attr for d in getattr(node, "decorator_list", [])
                 if isinstance(d, ast.Attribute) and d.attr in ACCESSORS
                 and isinstance(d.value, ast.Name) and d.value.id == node.name), "def")


def _repeats(body) -> list[str]:
    """The names ``body`` binds a second time, in body order, each once. A ``"def"`` is a repeat
    after any earlier def/class of its name, of any kind: it replaces the whole binding, so a
    plain ``def x`` after ``@x.setter def x`` drops the setter (#2536 review F4, where ``x =
    property(get_x)`` made the setter the first ``def x``). A setter or deleter is a repeat
    after one of its own kind (#2536 review F2)."""
    seen: dict[str, set[str]] = {}
    found = []
    for node in body:
        if not isinstance(node, DEFS):
            continue
        kind, earlier = _kind(node), seen.setdefault(node.name, set())
        if (earlier if kind == "def" else kind in earlier):
            found.append(node.name)
        earlier.add(kind)
    return list(dict.fromkeys(found))


def repeated(source: str) -> list[str]:
    """Each name a module body or a class body binds with a second def/class (``_repeats``)."""
    tree = ast.parse(source)
    scopes = [("", tree.body)] + [(f"{n.name}.", n.body) for n in ast.walk(tree)
                                  if isinstance(n, ast.ClassDef)]
    return [f"{prefix}{name}" for prefix, body in scopes for name in _repeats(body)]
