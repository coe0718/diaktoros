"""The CLI package (#676, #715): every command is the one ``cli`` dispatches, with no second copy
in ``cli/__init__.py``, and no command module imports the package back.

``cli/__init__.py`` is the parser (``register_cli``); each command lives in a submodule
(``cli/reports.py``, ...), re-exported by the package so ``cli.cmd_stats`` is
``cli.reports.cmd_stats``. Callers import submodules under their old names
(``from .cli import common as cli_common``).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import ast
import importlib
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import cli  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "diaktoros" / "cli"
MODULES = ("reports", "operations", "lifecycle", "install", "checks", "settings", "set_command",
           "apply_command", "triage_command", "identity_command")
SHARED = ("common", "route_sync", "set_adjudication")  # helper modules: no commands, never import cli


def _defined(path: pathlib.Path) -> set[str]:
    return {node.name for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


class Split(unittest.TestCase):
    def test_each_moved_command_is_the_one_cli_dispatches(self):
        in_cli = _defined(PACKAGE / "__init__.py")
        for name in MODULES:
            module = importlib.import_module(f"diaktoros.cli.{name}")
            commands = sorted(n for n in _defined(PACKAGE / f"{name}.py")
                              if n.startswith("cmd_"))
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(module=name, command=command):
                    self.assertIs(getattr(cli, command), getattr(module, command))
                    self.assertNotIn(command, in_cli, "a second copy is left in cli.py")

    def test_the_parser_dispatches_to_the_moved_code(self):
        class Ctx:
            def register_cli_command(self, name, summary, setup, **kwargs):
                if name == "dk":
                    self.setup = setup
        ctx = Ctx()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes dk")
        ctx.setup(parser)
        verbs = next(a for a in parser._actions
                     if isinstance(a, argparse._SubParsersAction)).choices
        for name in MODULES:
            module = importlib.import_module(f"diaktoros.cli.{name}")
            for command in (n for n in dir(module) if n.startswith("cmd_")):
                verb = command[4:].replace("_", "-")
                with self.subTest(verb=verb):
                    self.assertIs(verbs[verb].get_default("func"), getattr(module, command))

    def test_shared_helpers_have_one_binding(self):
        # cli calls them as cli_common.<name>: a second binding in cli would let a test's patch
        # reach some callers and silently miss others.
        from diaktoros.cli import common as cli_common
        for name in _defined(PACKAGE / "common.py"):
            with self.subTest(helper=name):
                self.assertFalse(hasattr(cli, name), f"cli binds {name}: call cli_common.{name}")
                self.assertTrue(callable(getattr(cli_common, name)))

    def test_no_test_patches_a_command_on_cli(self):
        # The parser dispatches each module's own cmd_*: a patch on cli's re-export would never
        # be called by `hermes dk <verb>`, so it would silently test nothing.
        pattern = re.compile(r"""patch\.object\(\s*cli\s*,\s*["']cmd_|["']diaktoros\.cli\.cmd_""")
        for path in sorted((ROOT / "tests").rglob("*.py")):
            with self.subTest(file=path.name):
                self.assertIsNone(pattern.search(path.read_text()), "patch it in its module")

    def test_no_command_module_imports_the_package(self):
        # A submodule may import its siblings (`from . import common`), never the package's own
        # names (`from . import cmd_status`, `from .. import cli`): that is the cycle back to
        # register_cli, and a second binding of whatever it imports.
        submodules = set(MODULES + SHARED)
        for name in MODULES + SHARED:
            tree = ast.parse((PACKAGE / f"{name}.py").read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                names = {alias.name for alias in node.names}
                with self.subTest(module=name, line=node.lineno):
                    if node.level == 1 and node.module is None:
                        self.assertLessEqual(names, submodules, "imports the package's names")
                    self.assertFalse(node.level == 2 and node.module is None and "cli" in names)
                    self.assertNotEqual(node.module, "diaktoros.cli")
                    self.assertFalse(node.level == 0 and node.module == "diaktoros"
                                     and "cli" in names)


if __name__ == "__main__":
    unittest.main()
