"""The config package split (#676): each moved group lives in one submodule, with one binding.

``config/__init__.py`` is the core every module imports; the groups that move out become
submodules (``config/settings_form.py`` first). A moved name must not stay reachable as
``config.<name>`` (a second binding a test could patch without reaching the code), a submodule
reaches the core only through the package (``from .. import config``, so a patch on a core rule
reaches it too), and the core never imports a submodule at module level (that would be a cycle:
the submodule imports the core).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config  # noqa: E402

PACKAGE = pathlib.Path(config.__file__).resolve().parent
SUBMODULES = ("settings_form", "credentials", "host_guards", "blocks")


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
            module = importlib.import_module(f"diaktoros.config.{name}")
            for moved in _top_level_names(PACKAGE / f"{name}.py"):
                with self.subTest(module=name, name=moved):
                    self.assertNotIn(moved, core, "defined in both the core and the submodule")
                    self.assertFalse(hasattr(config, moved), f"call {name}.{moved}")
                    self.assertTrue(hasattr(module, moved))

    def test_nothing_imports_a_moved_name_from_config(self):
        # `from .config import guard_network` inside a function is the form a rename sweep of
        # `config.X` misses; it would fail only when that function runs.
        moved = {name for sub in SUBMODULES for name in _top_level_names(PACKAGE / f"{sub}.py")}
        root = PACKAGE.parents[1]
        files = [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").glob("*.py"),
                 *(root / "tests").rglob("*.py"), root / "__init__.py"]
        for path in files:
            if path.parent == PACKAGE:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.ImportFrom) and node.module in ("config", "diaktoros.config")
                        and {alias.name for alias in node.names} & moved):
                    self.fail(f"{path.relative_to(root)}:{node.lineno} imports "
                              f"{sorted({a.name for a in node.names} & moved)} from config")

    def test_no_dynamic_or_chained_reference_to_a_moved_name(self):
        # The forms a `config.X` sweep misses, each of which once kept a test passing while it
        # checked nothing: getattr/hasattr by string (a fallback branch, or None), a chain through
        # another module (`routes.config.guard_network`), and a string patch target.
        import re
        moved = {name for sub in SUBMODULES for name in _top_level_names(PACKAGE / f"{sub}.py")}
        alt = "|".join(sorted(map(re.escape, moved), key=len, reverse=True))
        pattern = re.compile(rf"""\b(?:getattr|hasattr|setattr|delattr)\(\s*\w*config\w*\s*,\s*["']"""
                             rf"""(?:{alt})["']|\b\w+\.config\.(?:{alt})\b|["']diaktoros\.config\."""
                             rf"""(?:{alt})["']|patch\.object\(\s*[\w.]*config\s*,\s*["'](?:{alt})["']""")
        root = PACKAGE.parents[1]
        for path in [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").glob("*.py"),
                     *(root / "tests").rglob("*.py")]:
            if path.parent == PACKAGE or path.name == "test_config_modules.py":
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
                                  "`from .. import config`")

    def test_the_core_imports_no_submodule_at_module_level(self):
        tree = ast.parse((PACKAGE / "__init__.py").read_text())
        for node in tree.body:                       # module level only: a lazy import is fine
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                with self.subTest(line=node.lineno):
                    self.assertNotIn(node.module, SUBMODULES)
                    self.assertFalse({a.name for a in node.names} & set(SUBMODULES))


if __name__ == "__main__":
    unittest.main()
