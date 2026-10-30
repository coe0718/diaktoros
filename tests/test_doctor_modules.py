"""The doctor package split (#1349): each moved group of checks lives in one submodule, with one
binding, by the same rules as the config split (``test_config_modules``, #676).

``doctor/__init__.py`` is the core every check module imports; the groups that move out become
submodules (``doctor/hook_checks.py`` and ``doctor/route_checks.py`` first). A moved name must
not stay reachable as ``doctor.<name>`` (a second binding a test could patch without reaching
the code), a submodule reaches the core only through the package (``from .. import doctor``, so
a patch on a core name reaches it too), and the core never imports a submodule at module level
(that would be a cycle: the submodule imports the core).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import doctor  # noqa: E402

PACKAGE = pathlib.Path(doctor.__file__).resolve().parent
SUBMODULES = ("hook_checks", "route_checks", "identity_checks", "seat_checks", "cron_checks")


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
            module = importlib.import_module(f"diaktoros.doctor.{name}")
            for moved in _top_level_names(PACKAGE / f"{name}.py"):
                with self.subTest(module=name, name=moved):
                    self.assertNotIn(moved, core, "defined in both the core and the submodule")
                    self.assertFalse(hasattr(doctor, moved), f"call {name}.{moved}")
                    self.assertTrue(hasattr(module, moved))

    def test_nothing_imports_a_moved_name_from_doctor(self):
        # `from .doctor import check_hook` inside a function is the form a rename sweep of
        # `doctor.X` misses; it would fail only when that function runs.
        moved = {name for sub in SUBMODULES for name in _top_level_names(PACKAGE / f"{sub}.py")}
        root = PACKAGE.parents[1]
        files = [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").glob("*.py"),
                 *(root / "tests").rglob("*.py"), root / "__init__.py"]
        for path in files:
            if path.parent == PACKAGE:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.ImportFrom) and node.module in ("doctor", "diaktoros.doctor")
                        and {alias.name for alias in node.names} & moved):
                    self.fail(f"{path.relative_to(root)}:{node.lineno} imports "
                              f"{sorted({a.name for a in node.names} & moved)} from doctor")

    def test_no_dynamic_or_chained_reference_to_a_moved_name(self):
        # The forms a `doctor.X` sweep misses: getattr/hasattr by string, a chain through another
        # module (`cli.doctor.check_hook`), and a string patch target.
        import re
        moved = {name for sub in SUBMODULES for name in _top_level_names(PACKAGE / f"{sub}.py")}
        alt = "|".join(sorted(map(re.escape, moved), key=len, reverse=True))
        pattern = re.compile(rf"""\b(?:getattr|hasattr|setattr|delattr)\(\s*\w*doctor\w*\s*,\s*["']"""
                             rf"""(?:{alt})["']|\b\w+\.doctor\.(?:{alt})\b|["']diaktoros\.doctor\."""
                             rf"""(?:{alt})["']|patch\.object\(\s*[\w.]*doctor\w*\s*,\s*["'](?:{alt})["']""")
        root = PACKAGE.parents[1]
        for path in [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").glob("*.py"),
                     *(root / "tests").rglob("*.py")]:
            if path.parent == PACKAGE or path.name == "test_doctor_modules.py":
                continue
            found = pattern.search(path.read_text())
            with self.subTest(file=path.name):
                self.assertIsNone(found, found and f"{path.relative_to(root)}: {found.group(0)}")

    def test_a_submodule_reaches_the_core_through_the_package(self):
        for name in SUBMODULES:
            tree = ast.parse((PACKAGE / f"{name}.py").read_text())
            with self.subTest(module=name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 1 and not node.module:
                        self.fail(f"{name}: `from . import …` binds core names; use "
                                  "`from .. import doctor`")

    def test_the_core_imports_no_submodule_at_module_level(self):
        tree = ast.parse((PACKAGE / "__init__.py").read_text())
        for node in tree.body:                       # module level only: a lazy import is fine
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                with self.subTest(line=node.lineno):
                    self.assertNotIn(node.module, SUBMODULES)
                    self.assertFalse({a.name for a in node.names} & set(SUBMODULES))


if __name__ == "__main__":
    unittest.main()
