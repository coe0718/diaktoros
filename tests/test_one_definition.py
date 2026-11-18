"""No module or class binds one function or class name twice.

Two PRs can each move the same block into a helper of the same name at different places in a
file. Git merges both with no conflict, and Python keeps only the later ``def``, so the other
PR's rule is silently gone: ``explain._queue_blockers`` lost #2075's parked-PR rule that way, and
only a test of that one case caught it.

A ``def`` decorated with its own name's ``.setter``, ``.getter`` or ``.deleter`` builds on the
earlier binding instead of replacing it (a property and its setter), so it is not a second
definition (#2142). A second accessor of one kind is: a second ``@x.setter`` replaces the first
setter as silently as a second ``def`` (#2536 review F2), and ``@x.getter`` replaces the getter
``@property def x`` already gave, so it counts as a second ``def``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import collections
import pathlib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ACCESSORS = {"setter", "deleter"}      # a getter replaces the property's own: a "def"


def _kind(node) -> str:
    """How ``node`` binds its name: ``"setter"`` or ``"deleter"`` for ``@x.setter def x`` and
    ``@x.deleter def x``, which extend the property ``x`` already names rather than replacing it
    (#2142); ``"def"`` for anything else, ``@x.getter`` included, since it replaces the getter
    ``@property def x`` gave. Each kind is counted apart, so a property with a setter and a deleter
    is one definition, and a repeat of any one kind is a repeat (#2536 review F2: a second
    ``@x.setter`` replaced the first setter and the scan let it pass)."""
    return next((d.attr for d in getattr(node, "decorator_list", [])
                 if isinstance(d, ast.Attribute) and d.attr in ACCESSORS
                 and isinstance(d.value, ast.Name) and d.value.id == node.name), "def")


def repeated(source: str) -> list[str]:
    """Each name a module body or a class body binds with more than one def/class of one kind
    (``_kind``)."""
    tree = ast.parse(source)
    scopes = [("", tree.body)] + [(f"{n.name}.", n.body) for n in ast.walk(tree)
                                  if isinstance(n, ast.ClassDef)]
    found = []
    for prefix, body in scopes:
        counts = collections.Counter((n.name, _kind(n)) for n in body if isinstance(n, DEFS))
        found += [f"{prefix}{name}" for name in dict.fromkeys(
            name for (name, _), k in counts.items() if k > 1)]
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

    def test_a_second_accessor_of_one_kind_is_a_repeat(self):
        # #2536 review F2: the second ``@x.setter`` replaces the first setter, so only one of
        # the two runs; every self-named accessor used to be left out of the count.
        prop = "class C:\n @property\n def x(self): return 1\n @x.setter\n def x(self, v): pass\n"
        self.assertEqual(repeated(prop + " @x.setter\n def x(self, v): print('replacement')\n"),
                         ["C.x"])
        twice = " @x.deleter\n def x(self): pass\n" * 2
        self.assertEqual(repeated(prop + twice), ["C.x"])
        # ``@x.getter`` replaces the getter ``@property def x`` gave: a second getter.
        self.assertEqual(repeated(prop + " @x.getter\n def x(self): return 2\n"), ["C.x"])
        # A second property under the name is a second plain def, reported once.
        self.assertEqual(repeated(prop + prop[9:]), ["C.x"])

    def test_the_scan_would_report_a_property_without_the_accessor_rule(self):
        # #2536 review F1: the scan and this test share a file, so the change proof's base run
        # (the PR's tests on the base) has the rule too; undoing it here shows it is needed.
        prop = "class C:\n @property\n def x(self): pass\n @x.setter\n def x(self, v): pass\n"
        with mock.patch(f"{__name__}._kind", lambda node: "def"):    # the base: every def
            self.assertEqual(repeated(prop), ["C.x"])
        self.assertEqual(repeated(prop), [])


if __name__ == "__main__":
    unittest.main()
