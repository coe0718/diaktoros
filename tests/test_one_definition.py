"""No module or class binds one function or class name twice.

Two PRs can each move the same block into a helper of the same name at different places in a
file. Git merges both with no conflict, and Python keeps only the later ``def``, so the other
PR's rule is silently gone: ``explain._queue_blockers`` lost #2075's parked-PR rule that way, and
only a test of that one case caught it.

A ``def`` decorated with its own name's ``.setter``, ``.getter`` or ``.deleter`` builds on the
earlier binding instead of replacing it (a property and its setter), so it is not a second
definition (#2142).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import collections
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ACCESSORS = {"setter", "getter", "deleter"}


def _extends(node) -> bool:
    """``@x.setter def x`` (or ``.getter``/``.deleter``): it extends the property ``x`` already
    names rather than replacing it (#2142)."""
    return any(isinstance(d, ast.Attribute) and d.attr in ACCESSORS
               and isinstance(d.value, ast.Name) and d.value.id == node.name
               for d in getattr(node, "decorator_list", []))


def repeated(source: str) -> list[str]:
    """Each name a module body or a class body binds with more than one def/class."""
    tree = ast.parse(source)
    scopes = [("", tree.body)] + [(f"{n.name}.", n.body) for n in ast.walk(tree)
                                  if isinstance(n, ast.ClassDef)]
    found = []
    for prefix, body in scopes:
        counts = collections.Counter(n.name for n in body
                                     if isinstance(n, DEFS) and not _extends(n))
        found += [f"{prefix}{name}" for name, k in counts.items() if k > 1]
    return found


class OneDefinition(unittest.TestCase):
    def test_no_name_is_defined_twice_in_one_scope(self):
        found = {}
        for root in ("diaktoros", "tests"):
            for path in sorted((ROOT / root).rglob("*.py")):
                if names := repeated(path.read_text(encoding="utf-8")):
                    found[str(path.relative_to(ROOT))] = names
        self.assertEqual(found, {}, "a later def silently replaces the earlier one: keep one, "
                                    "with both rules")

    def test_the_scan_finds_a_repeat_in_a_module_and_in_a_class(self):
        self.assertEqual(repeated("def f(): pass\nx = 1\ndef f(): return 1\n"), ["f"])
        self.assertEqual(repeated("class C:\n def m(self): pass\n def m(self): pass\n"), ["C.m"])
        self.assertEqual(repeated("def f(): pass\nclass C:\n def f(self): pass\n"), [])

    def test_a_property_and_its_accessors_are_one_definition(self):
        # #2142: ``@x.setter`` builds on the property ``x``; it does not replace it.
        prop = "class C:\n @property\n def x(self): pass\n @x.setter\n def x(self, v): pass\n"
        self.assertEqual(repeated(prop), [])
        self.assertEqual(repeated(prop + " @x.deleter\n def x(self): pass\n"), [])
        # A second plain def, or another name's setter, still replaces it.
        self.assertEqual(repeated(prop + " def x(self): pass\n"), ["C.x"])
        self.assertEqual(repeated(prop + " @property\n def y(self): pass\n"
                                         " @y.setter\n def x(self, v): pass\n"), ["C.x"])


if __name__ == "__main__":
    unittest.main()
