"""The run_supervisor package split (#717): each moved group lives in one submodule, with one
binding, and nothing in the package shadows the submodule it calls.

``run_supervisor/__init__.py`` is the core every module imports; groups that move out become
submodules (``pr_facts`` first) that the core calls as ``pr_facts.<name>``. A moved name must not
stay reachable as ``run_supervisor.<name>`` (a second binding a test could patch without
reaching the code), nothing may import a moved name from the package, and no function may have
a local variable named like a submodule it calls: ``facts.pr_record`` once resolved to a local
``facts`` dict in ``isolated_prompt``, which is why the submodule is ``pr_facts``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib
import pathlib
import symtable
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import run_supervisor  # noqa: E402

PACKAGE = pathlib.Path(run_supervisor.__file__).resolve().parent
ROOT = PACKAGE.parents[1]
SUBMODULES = ("pr_facts", "turn_prompts", "records")


def _top_level_names(path: pathlib.Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class Split(unittest.TestCase):
    def test_a_moved_name_has_one_home(self):
        core = _top_level_names(PACKAGE / "__init__.py")
        for name in SUBMODULES:
            module = importlib.import_module(f"diaktoros.run_supervisor.{name}")
            for moved in _top_level_names(PACKAGE / f"{name}.py"):
                with self.subTest(module=name, name=moved):
                    self.assertNotIn(moved, core)
                    self.assertFalse(hasattr(run_supervisor, moved), f"call {name}.{moved}")
                    self.assertTrue(hasattr(module, moved))

    def test_the_record_methods_live_only_in_the_mixin(self):
        # Supervisor inherits records.LedgerRecords: a method defined again in Supervisor's own
        # body would silently shadow the mixin's (and a test patching one would miss the other).
        from diaktoros.run_supervisor import Supervisor, records
        self.assertIn(records.LedgerRecords, Supervisor.__mro__)
        mixin = {name for name in vars(records.LedgerRecords) if not name.startswith("__")}
        self.assertTrue(mixin)
        self.assertFalse(mixin & set(vars(Supervisor)), "defined on both Supervisor and the mixin")

    def test_nothing_imports_a_moved_name_from_the_package(self):
        moved = {n for sub in SUBMODULES for n in _top_level_names(PACKAGE / f"{sub}.py")}
        files = [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py"),
                 *(ROOT / "tests").rglob("*.py")]
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.ImportFrom)
                        and node.module in ("run_supervisor", "diaktoros.run_supervisor")
                        and {alias.name for alias in node.names} & moved):
                    self.fail(f"{path.relative_to(ROOT)}:{node.lineno} imports "
                              f"{sorted({a.name for a in node.names} & moved)} from the package")

    def test_no_function_shadows_a_submodule_it_calls(self):
        for path in PACKAGE.glob("*.py"):
            top = symtable.symtable(path.read_text(), str(path), "exec")

            def walk(table):
                for child in table.get_children():
                    if child.get_type() == "function":
                        for sub in SUBMODULES:
                            symbol = child.lookup(sub) if sub in child.get_identifiers() else None
                            with self.subTest(file=path.name, function=child.get_name(), name=sub):
                                self.assertFalse(symbol is not None and symbol.is_local()
                                                 and not symbol.is_imported(),
                                                 f"a local `{sub}` shadows the submodule")
                    walk(child)
            walk(top)


if __name__ == "__main__":
    unittest.main()
