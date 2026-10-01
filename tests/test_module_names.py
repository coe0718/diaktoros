"""Guard: no module-level name in ``review_loop/`` or ``scripts/`` is bound twice by assignment.

A second top-level ``_PROBE = ...`` in ``review_loop/selftest.py`` (the model prompt) once
silently replaced the sandbox probe program defined earlier in the same module, so the live
sandbox step ran an English sentence as Python. Python never warns about a rebinding, so this
test does.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (relative path, name) pairs that rebind on purpose. Each entry needs a comment saying why.
# Empty: nothing in the package legitimately rebinds a module-level name today.
ALLOWED: set[tuple[str, str]] = set()


def _bound_names(target: ast.expr):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def duplicate_assignments(source: str) -> dict[str, list[int]]:
    """Module-level names bound by more than one plain (or annotated) assignment."""
    seen: dict[str, list[int]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            names = [name for target in node.targets for name in _bound_names(target)]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names = list(_bound_names(node.target))
        else:
            continue
        for name in names:
            seen.setdefault(name, []).append(node.lineno)
    return {name: lines for name, lines in seen.items() if len(lines) > 1}


def _modules():
    return sorted([*ROOT.glob("review_loop/*.py"), *ROOT.glob("scripts/*.py")])


class ModuleLevelNames(unittest.TestCase):
    def test_scan_covers_the_package(self):
        names = {path.relative_to(ROOT).as_posix() for path in _modules()}
        self.assertIn("review_loop/selftest.py", names)
        self.assertTrue(any(name.startswith("scripts/") for name in names))

    def test_the_scan_catches_a_rebinding(self):
        self.assertEqual(duplicate_assignments("A = 1\nB = 2\nA, C = 3, 4\n"), {"A": [1, 3]})
        self.assertEqual(duplicate_assignments("X: int = 1\nX = 2\n"), {"X": [1, 2]})
        self.assertEqual(duplicate_assignments("def f():\n    A = 1\n    A = 2\n"), {})

    def test_no_module_level_name_is_assigned_twice(self):
        found = []
        for path in _modules():
            rel = path.relative_to(ROOT).as_posix()
            for name, lines in duplicate_assignments(path.read_text()).items():
                if (rel, name) not in ALLOWED:
                    found.append(f"{rel}: {name} assigned on lines {lines}")
        self.assertEqual(found, [], "a later module-level assignment silently replaces an "
                                    "earlier one; give each value its own name")


if __name__ == "__main__":
    unittest.main()
