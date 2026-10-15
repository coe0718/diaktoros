"""The CLI split into cohesive modules (#676): every moved command is the one ``cli`` dispatches,
with no second copy left behind, and no moved module imports ``cli`` back.

Each extraction adds its module to ``MODULES``. A command that moves is re-exported by ``cli``
(``cli.cmd_stats`` is ``cli_reports.cmd_stats``), so tests and the parser reach the moved code.
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
MODULES = ("cli_reports", "cli_operations", "cli_lifecycle", "cli_install", "cli_checks",
           "cli_settings")
SHARED = ("cli_common",)          # helpers several groups use: no commands, never imports cli


def _defined(path: pathlib.Path) -> set[str]:
    return {node.name for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


class Split(unittest.TestCase):
    def test_each_moved_command_is_the_one_cli_dispatches(self):
        in_cli = _defined(ROOT / "diaktoros" / "cli.py")
        for name in MODULES:
            module = importlib.import_module(f"diaktoros.{name}")
            commands = sorted(n for n in _defined(ROOT / "diaktoros" / f"{name}.py")
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
            module = importlib.import_module(f"diaktoros.{name}")
            for command in (n for n in dir(module) if n.startswith("cmd_")):
                verb = command[4:].replace("_", "-")
                with self.subTest(verb=verb):
                    self.assertIs(verbs[verb].get_default("func"), getattr(module, command))

    def test_shared_helpers_have_one_binding(self):
        # cli calls them as cli_common.<name>: a second binding in cli would let a test's patch
        # reach some callers and silently miss others.
        from diaktoros import cli_common
        for name in _defined(ROOT / "diaktoros" / "cli_common.py"):
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

    def test_no_moved_module_imports_cli(self):
        for name in MODULES + SHARED:
            tree = ast.parse((ROOT / "diaktoros" / f"{name}.py").read_text())
            imported = {alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom) for alias in node.names}
            imported |= {node.module or "" for node in ast.walk(tree)
                         if isinstance(node, ast.ImportFrom)}
            with self.subTest(module=name):
                self.assertNotIn("cli", imported)


if __name__ == "__main__":
    unittest.main()
