"""Doctor reads a profile's ``.env`` key names through Hermes's own tokenizer (#2374).

The Hermes catalog review asked plugins onto ``agent.secret_scope``. Doctor's ``GH_TOKEN`` warning
only needs to know whether a key is set, so it parses the file with
``agent.secret_scope.load_env_file`` (the one tokenizer every Hermes reader uses) when Hermes is
importable, and falls back to its own line reader when it is not. Either way the warning names the
key and the file, never the value.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from diaktoros import doctor  # noqa: E402
from diaktoros.doctor import identity_checks  # noqa: E402

SECRET = "ghp_never_printed_0123456789"


def hermes_scope(parsed: dict | Exception, calls: list):
    """``agent.secret_scope`` as Hermes ships it, as far as doctor uses it: ``load_env_file``."""
    def load_env_file(path):
        calls.append(path)
        if isinstance(parsed, Exception):
            raise parsed
        return dict(parsed)
    scope = types.ModuleType("agent.secret_scope")
    scope.load_env_file = load_env_file
    agent = types.ModuleType("agent")
    agent.secret_scope = scope
    return mock.patch.dict(sys.modules, {"agent": agent, "agent.secret_scope": scope})


NO_HERMES = mock.patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None})


class EnvKeys(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = self.home / "profiles" / "fixer" / ".env"
        self.env.parent.mkdir(parents=True)
        self.loop = {"seats": {"fixer": {"profile": "fixer", "login": ""}}}

    def test_hermes_tokenizer_decides_when_it_is_importable(self):
        # A line the fallback reader gets wrong both ways: `export` hides the key from it, and a
        # quoted empty value looks set to it. Hermes's tokenizer reads both as Hermes does.
        self.env.write_text(f'export GH_TOKEN="{SECRET}"\nEMPTY=""\n')
        calls: list = []
        with hermes_scope({"GH_TOKEN": SECRET, "EMPTY": ""}, calls):
            self.assertEqual(doctor._env_keys(self.env), {"GH_TOKEN"})
        self.assertEqual(calls, [self.env])
        with NO_HERMES:                                    # what the line reader makes of it
            self.assertEqual(doctor._env_keys(self.env), {"export GH_TOKEN", "EMPTY"})

    def test_without_hermes_the_line_reader_is_unchanged(self):
        self.env.write_text(f"# a comment\nGH_TOKEN={SECRET}\nBLANK=\nNOTE= # only a comment\n")
        with NO_HERMES:
            self.assertEqual(doctor._env_keys(self.env), {"GH_TOKEN"})
            self.assertEqual(doctor._env_keys(self.home / "missing.env"), set())

    def test_a_tokenizer_that_raises_reads_as_no_keys(self):
        self.env.write_text(f"GH_TOKEN={SECRET}\n")
        with hermes_scope(OSError("unreadable"), []):
            self.assertEqual(doctor._env_keys(self.env), set())

    def test_the_gh_token_warning_names_the_file_never_the_value(self):
        cases = (("set", {"GH_TOKEN": SECRET}, True), ("unset", {"OTHER": "x"}, False),
                 ("unreadable", OSError("no"), False))
        for name, parsed, warned in cases:
            with self.subTest(case=name):
                self.env.write_text(f"GH_TOKEN={SECRET}\n")
                with hermes_scope(parsed, []):
                    check = identity_checks.check_credential(self.loop, "fixer")
                self.assertEqual(f"nonempty GH_TOKEN in {self.env}" in check.detail, warned,
                                 check.detail)
                self.assertNotIn(SECRET, f"{check.detail} {check.fix}")


if __name__ == "__main__":
    unittest.main()
