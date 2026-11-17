"""The state package (#1085): ``state.py`` is ``state/__init__.py``, and ``ledger``, ``migrate``,
``backup``, ``backup_fresh`` and ``restore_checks`` are modules of it.

Two of them find the plugin from their own file. The backup imports every lock writer it finds by
walking the plugin (``backup._load_writers``), and a ledger lock timeout is named after the
innermost plugin function it passed through (``ledger.where``). One level down, either would see
only ``state/`` and fail quietly: a backup would archive the other writers' lock files as data,
and a timeout elsewhere would lose its ``(during …)``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import state  # noqa: E402
from diaktoros.state import backup, ledger  # noqa: E402

MOVED = ("ledger", "migrate", "backup", "backup_fresh", "restore_checks")


class Package(unittest.TestCase):
    def test_the_state_is_the_package_not_a_flat_module(self):
        self.assertEqual(pathlib.Path(state.__file__), ROOT / "diaktoros" / "state" / "__init__.py")
        self.assertEqual(list(state.__path__), [str(ROOT / "diaktoros" / "state")])
        self.assertFalse((ROOT / "diaktoros" / "state.py").exists())

    def test_the_states_modules_are_the_packages_own(self):
        # #1085 step 2: each lives in the package, and no flat copy is left to shadow it.
        import importlib
        for name in MOVED:
            with self.subTest(name):
                module = importlib.import_module(f"diaktoros.state.{name}")
                self.assertEqual(pathlib.Path(module.__file__), ROOT / "diaktoros" / "state" / f"{name}.py")
                self.assertIs(getattr(state, name), module)
                self.assertFalse((ROOT / "diaktoros" / f"{name}.py").exists())

    def test_the_backups_writer_scan_imports_the_whole_plugin(self):
        # The locks are registered anyway once other imports load their writers, so the scan's
        # own reach is checked by what it imports: writers in and outside state/, from the
        # plugin package, and never the backup itself.
        with mock.patch.object(backup.importlib, "import_module") as imported:
            backup._load_writers()
        calls = imported.call_args_list
        for name in (".state", ".observer", ".gate.decisions"):
            with self.subTest(name):
                self.assertIn(mock.call(name, "diaktoros"), calls)
        self.assertNotIn(mock.call(".state.__init__", "diaktoros"), calls)
        self.assertFalse([c for c in calls if c.args[0] in (".state.backup", ".backup", ".util")])
        self.assertEqual({c.args[1] for c in calls}, {"diaktoros"})

    def test_a_lock_timeout_outside_the_state_package_is_named(self):
        source = ROOT / "diaktoros" / "observer.py"
        namespace = {"sqlite3": sqlite3}
        exec(compile("def an_observer_write():\n"
                     "    raise sqlite3.OperationalError('database is locked')\n",
                     str(source), "exec"), namespace)
        try:
            namespace["an_observer_write"]()
        except sqlite3.OperationalError as exc:
            self.assertEqual(ledger.where(exc), " (during an_observer_write)")
        else:
            self.fail("no lock timeout")


if __name__ == "__main__":
    unittest.main()
