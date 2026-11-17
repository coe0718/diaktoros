"""The state package (#1085): ``state.py`` is ``state/__init__.py``.

Every importer names it ``diaktoros.state``, which resolves to the package unchanged. The backup
finds the plugin's lock writers by walking its files (``backup._load_writers``), so the state
locks must still be found one level down, or a backup would archive them as data.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import backup, state  # noqa: E402


class Package(unittest.TestCase):
    def test_the_state_is_the_package_not_a_flat_module(self):
        self.assertEqual(pathlib.Path(state.__file__), ROOT / "diaktoros" / "state" / "__init__.py")
        self.assertEqual(list(state.__path__), [str(ROOT / "diaktoros" / "state")])
        self.assertFalse((ROOT / "diaktoros" / "state.py").exists())

    def test_the_backups_writer_scan_imports_the_package(self):
        # The state locks are registered anyway once another import loads ``state``, so the
        # scan's own reach is checked by what it imports.
        with mock.patch.object(backup.importlib, "import_module") as imported:
            backup._load_writers()
        self.assertIn(mock.call(".state", "diaktoros"), imported.call_args_list)
        self.assertNotIn(mock.call(".state.__init__", "diaktoros"), imported.call_args_list)


if __name__ == "__main__":
    unittest.main()
