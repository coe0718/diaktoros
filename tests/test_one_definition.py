"""No module or class binds one function or class name twice.

Two PRs can each move the same block into a helper of the same name at different places in a
file. Git merges both with no conflict, and Python keeps only the later ``def``, so the other
PR's rule is silently gone: ``explain._queue_blockers`` lost #2075's parked-PR rule that way, and
only a test of that one case caught it.

A ``def`` decorated with its own name's ``.setter``, ``.getter`` or ``.deleter`` builds on the
earlier binding instead of replacing it (a property and its setter), so it is not a second
definition (#2142). A second accessor of one kind is: a second ``@x.setter`` replaces the first
setter as silently as a second ``def`` (#2536 review F2), and ``@x.getter`` replaces the getter
``@property def x`` already gave, so it counts as a second ``def``. Each name is walked in body
order: a plain ``def`` after a setter replaces it however the property was made (#2536 review F4).

The scan is ``tools/one_definition.py``; see there why it is not in this file.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The scan lives in tools/one_definition.py, outside tests/, so the host's base-failure proof runs
# the tests below against the base's scan, not this PR's (#1973, #2536 review F1).
sys.path.insert(0, str(ROOT / "tools"))
import one_definition  # noqa: E402
from one_definition import repeated  # noqa: E402


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
        # #2536 review F1: undoing the accessor rule shows it is needed (the base proof now runs
        # the base's scan, from tools/, but this still pins what the rule does).
        prop = "class C:\n @property\n def x(self): pass\n @x.setter\n def x(self, v): pass\n"
        with mock.patch.object(one_definition, "_kind", lambda node: "def"):  # every def
            self.assertEqual(repeated(prop), ["C.x"])
        self.assertEqual(repeated(prop), [])

    def test_a_plain_def_after_a_setter_is_a_repeat(self):
        # #2536 review F4: kinds counted apart, ``x = property(get_x)`` left the setter the first
        # ``def x``, so the plain ``def x`` after it was one "def" and replaced the setter unseen.
        assigned = ("class C:\n    def get_x(self): return 1\n    x = property(get_x)\n"
                    "    @x.setter\n    def x(self, value): print('setter')\n"
                    "    def x(self): return 'replacement'\n")
        self.assertEqual(repeated(assigned), ["C.x"])
        decorated = ("class C:\n @property\n def x(self): return 1\n"
                     " @x.setter\n def x(self, v): pass\n def x(self): return 2\n")
        self.assertEqual(repeated(decorated), ["C.x"])
        # A setter, then a deleter, then another name: no repeat; a second getter still is.
        self.assertEqual(repeated(assigned.replace("    def x(self): return 'replacement'\n",
                                                   "    @x.deleter\n    def x(self): pass\n")),
                         [])
        self.assertEqual(repeated(decorated.replace(" def x(self): return 2\n",
                                                    " @x.getter\n def x(self): return 2\n")),
                         ["C.x"])

    def test_a_base_proof_tests_the_base_scan(self):
        # #2536 review F1: the scan lives off the test paths, so the host's base-failure proof
        # (change_proof.apply_tests) runs the head's tests above against the base's scan. It fails
        # if the scan goes back into a tests/ module, where it was.
        import _base_proof
        _base_proof.assert_taken_from_the_base(self, one_definition.__file__)


if __name__ == "__main__":
    unittest.main()
