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
FILES = [ROOT / "scripts" / "watchdog.py", ROOT / "diaktoros" / "cli.py",
         *sorted((ROOT / "scripts").glob("gate_*.py"))]


def crowded(source):
    """(line, names) for each package import that puts more than three names on one line."""
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module == "diaktoros" or (node.module is None and node.level == 1)):
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


if __name__ == "__main__":
    unittest.main()
