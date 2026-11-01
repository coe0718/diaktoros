"""The watchdog split (#1349): each moved group lives in one module, with one binding.

``scripts/watchdog.py`` is the sweep; the groups that move out become ``diaktoros`` modules
(``watch_mode`` — the test mode and clocks every piece reads — then ``watch_health``,
``watch_conflicts`` and ``watch_stacked``). A moved name must not stay reachable through the
watchdog (a test patching ``watchdog.TEST`` would then miss every moved reader), and nothing
imports a moved name by itself: ``TEST`` copied into another module is exactly the binding a
patch never reaches.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib.util
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import watch_mode  # noqa: E402

MODULES = ("watch_mode", "watch_health", "watch_conflicts", "watch_stacked")
WATCHDOG = ROOT / "scripts" / "watchdog.py"


def _top_level_names(path: pathlib.Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _moved() -> set[str]:
    return {name for module in MODULES
            for name in _top_level_names(ROOT / "diaktoros" / f"{module}.py")}


def _sources():
    for path in [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py"),
                 *(ROOT / "tests").rglob("*.py")]:
        if path.name != "test_watchdog_modules.py":
            yield path


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("watchdog_modules_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Split(unittest.TestCase):
    def test_a_moved_name_has_one_home(self):
        watchdog = _load_watchdog()
        defined = _top_level_names(WATCHDOG)
        for moved in sorted(_moved()):
            with self.subTest(name=moved):
                self.assertNotIn(moved, defined, "defined in the watchdog and a moved module")
                self.assertFalse(hasattr(watchdog, moved), f"read it as watch_*.{moved}")

    def test_nothing_imports_a_moved_name_by_itself(self):
        moved = _moved()
        for path in _sources():
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.ImportFrom) and node.module
                        and node.module.split(".")[-1] in MODULES
                        and {alias.name for alias in node.names} & moved):
                    self.fail(f"{path.relative_to(ROOT)}:{node.lineno} imports "
                              f"{sorted({a.name for a in node.names} & moved)} by name")

    def test_no_reference_reaches_a_moved_name_through_the_watchdog(self):
        # Every form the tests bind the watchdog by: the module, `wd`/`_wd`, `fg.watchdog`,
        # `self.watchdog`, a patch.object or getattr by string, and a dotted patch target.
        alt = "|".join(sorted(map(re.escape, _moved()), key=len, reverse=True))
        alias = r"(?:[\w.]*\.)?(?:watchdog|wd|_wd)"
        pattern = re.compile(
            rf"""(?<![\w.]){alias}\.(?:{alt})\b"""
            rf"""|\b(?:patch\.object|getattr|hasattr|setattr|delattr)\(\s*{alias}\s*,\s*"""
            rf"""["'](?:{alt})["']"""
            rf"""|["']scripts\.watchdog\.(?:{alt})["']""")
        for path in _sources():
            found = pattern.search(path.read_text())
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertIsNone(found, found and found.group(0))

    def test_a_patched_test_mode_reaches_the_watchdog_and_the_moved_code(self):
        watchdog = _load_watchdog()
        watch, failed = {}, watchdog.watch_health._read_failed
        with mock.patch.object(watch_mode, "TEST", True):
            self.assertTrue(watchdog.watch_mode.TEST)
            # zero cooldown in test mode: the second read-failure alert is due at once
            loop = {"id": "one", "repo": "o/r", "cooldown_h": 6}
            first = failed(loop, watch, 100.0, "bot", "HTTP 401", 401, "")
            again = failed(loop, watch, 101.0, "bot", "HTTP 401", 401, "")
        self.assertTrue(first[1] and again[1], (first, again))
        with mock.patch.object(watch_mode, "TEST", False):
            later = failed(loop, watch, 102.0, "bot", "HTTP 401", 401, "")
        self.assertFalse(later[1], later)                     # the real six-hour cooldown


if __name__ == "__main__":
    unittest.main()
