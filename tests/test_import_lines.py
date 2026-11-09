"""Hot files keep one import per line once a statement names more than three modules (#552).

A long `from diaktoros import a, b, c, ...` line is edited by every PR that adds a module, so any
two such PRs conflict. This fails when the long line comes back.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAX_ON_ONE_LINE = 3
FILES = [ROOT / "scripts" / "watchdog.py", ROOT / "diaktoros" / "cli" / "__init__.py",
         ROOT / "diaktoros" / "cli" / "settings.py",  # every new setting edits it (#676)
         ROOT / "diaktoros" / "cli" / "set_command.py",   # and these since the split (#1349)
         ROOT / "diaktoros" / "cli" / "set_parser.py",
         ROOT / "diaktoros" / "cli" / "apply_command.py",
         ROOT / "diaktoros" / "cli" / "identity_command.py",
         ROOT / "diaktoros" / "cli" / "set_adjudication.py",
         ROOT / "diaktoros" / "cli" / "triage_command.py",
         *sorted((ROOT / "scripts").glob("gate_*.py"))]


def crowded(source):
    """(line, names) for each package import that puts more than three names on one line.

    A package import is `from diaktoros import ...` or a relative import that names modules
    directly: `from . import ...` and also `from .. import ...`, the form the cli modules use
    for the package root (#1457).
    """
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module == "diaktoros" or (node.module is None and node.level >= 1)):
            continue
        per_line = {}
        for alias in node.names:
            per_line.setdefault(alias.lineno, []).append(alias.name)
        for names in per_line.values():
            if len(names) > MAX_ON_ONE_LINE:
                bad.append((node.lineno, names))
    return bad


class ImportLines(unittest.TestCase):
    def test_files_found(self):
        self.assertGreaterEqual(len(FILES), 6)

    def test_no_long_package_import_line(self):
        for path in FILES:
            with self.subTest(file=path.name):
                self.assertEqual(crowded(path.read_text()), [],
                                 f"{path.name}: put one module per line")

    def test_detector_catches_a_long_line(self):
        self.assertEqual(len(crowded("from diaktoros import a, b, c, d\n")), 1)
        self.assertEqual(len(crowded("from . import a, b, c, d\n")), 1)
        self.assertEqual(crowded("from diaktoros import a, b, c\n"), [])

    def test_detector_catches_a_long_root_import_in_a_cli_module(self):
        # #1457: identity_command and set_adjudication import the root as `from .. import (...)`;
        # a fourth name on that line must fail like it does for `from . import`.
        self.assertEqual(len(crowded("from .. import config, util, gate_shims, route_intent\n")), 1)
        self.assertEqual(crowded("from .. import (\n    config,\n    util,\n    gate_shims,\n"
                                 "    route_intent,\n)\n"), [])

    def test_crowding_a_guarded_cli_module_fails(self):
        # Through the guard's own FILES: a crowded root import planted in each module #1457 adds.
        for name in ("identity_command.py", "set_adjudication.py"):
            with self.subTest(file=name):
                path = next((p for p in FILES if p.name == name), None)
                self.assertIsNotNone(path, f"{name} must be in FILES")
                planted = path.read_text() + "\nfrom .. import config, util, gate_shims, routes\n"
                self.assertEqual(len(crowded(planted)), 1)


if __name__ == "__main__":
    unittest.main()
