"""The broker_ipc split (#1349): RunBroker's issue writes live in ``broker_issues``, inherited.

``broker_issues.IssueWrites`` holds the methods; ``RunBroker`` inherits them, so a patch on
``RunBroker.<method>`` still reaches every caller. A moved name must not stay defined in
``broker_ipc`` too, the mixin reads ``broker_ipc``'s shared names through the module (so a patch
there reaches it) rather than importing them by name, and either module may be imported first.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker_ipc, broker_issues  # noqa: E402

CORE, MIXIN = ROOT / "diaktoros" / "broker_ipc.py", ROOT / "diaktoros" / "broker_issues.py"


def _defined(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, ast.ClassDef):
                names.update(f"{node.name}.{m.name}" for m in node.body
                             if isinstance(m, ast.FunctionDef))
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


class Split(unittest.TestCase):
    def test_the_issue_writes_have_one_home_and_are_inherited(self):
        core, mixin = _defined(ast.parse(CORE.read_text())), _defined(ast.parse(MIXIN.read_text()))
        methods = {name.split(".", 1)[1] for name in mixin if name.startswith("IssueWrites.")}
        self.assertTrue(methods)
        for method in sorted(methods):
            with self.subTest(method=method):
                self.assertNotIn(f"RunBroker.{method}", core, "defined in both modules")
                self.assertIs(getattr(broker_ipc.RunBroker, method),
                              getattr(broker_issues.IssueWrites, method))
        for name in sorted(n for n in mixin if "." not in n and n != "IssueWrites"):
            with self.subTest(name=name):
                self.assertNotIn(name, core)
                self.assertFalse(hasattr(broker_ipc, name), f"call broker_issues.{name}")

    def test_the_mixin_imports_no_broker_ipc_name_by_itself(self):
        for node in ast.walk(ast.parse(MIXIN.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("broker_ipc"):
                self.fail(f"broker_issues.py:{node.lineno} imports "
                          f"{[a.name for a in node.names]} from broker_ipc by name")

    def test_either_module_may_be_imported_first(self):
        for first in ("broker_issues", "broker_ipc"):
            with self.subTest(first=first):
                probe = (f"import diaktoros.{first}\nfrom diaktoros import broker_ipc\n"
                         "print(broker_ipc.RunBroker.__mro__[1].__name__)")
                out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, text=True,
                                     capture_output=True, timeout=60)
                self.assertEqual(out.stdout.strip(), "IssueWrites", out.stderr)


if __name__ == "__main__":
    unittest.main()
