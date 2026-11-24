"""#2450: a reader whose login is spelled ``LOGIN`` is a real login, not the placeholder.

``set --app`` takes only an App's bot login (``<app-slug>[bot]``), so a reader that is a person's
account is told to name the App's bot login as the reader (#1912). The reader fix used to treat
the literal ``LOGIN`` as its own stand-in, so a reader actually named ``LOGIN`` was offered
``--app LOGIN=...``, which ``set --app`` refuses. Only a caller with no login shows the stand-in.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_reader_identity as ri  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.config import credentials  # noqa: E402

RENAME = "--read-token <app-slug>[bot] --app <app-slug>[bot]=APP_ID:/abs/key.pem"


class ReaderNamedLogin(ri._Loop):
    def test_set_read_token_login_is_told_the_bot_login_rename(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        out = self.refused(["set", "--loop", "widgets", "--read-token", "LOGIN"],
                           r"no token file mapped for the reader 'LOGIN'")
        self.assertNotIn("--app LOGIN=", out)
        self.assertIn(RENAME, out)
        self.assertIn("'LOGIN' is not an App's bot login", out)
        # What the old advice named, `set --app` refuses.
        with mock.patch("builtins.print"):
            self.assertIsNone(cli_common._split_app_pair("LOGIN=123:/abs/key.pem"))

    def test_the_reader_fix_for_a_login_named_login(self):
        fix = credentials.reader_fix({"id": "widgets"}, "LOGIN")
        self.assertIn("--read-token LOGIN --token LOGIN=/path/to/pat", fix)
        self.assertNotIn("--app LOGIN=", fix)
        self.assertIn(RENAME, fix)

    def test_the_stand_in_still_offers_the_same_login_app(self):
        # No reader named yet (selftest, a loop file without read_token): the stand-in word.
        fix = credentials.reader_fix({"id": "widgets"}, stand_in="LOGIN")
        self.assertEqual(fix, "hermes dk set --loop widgets --read-token LOGIN --token "
                              "LOGIN=/path/to/pat (or --app LOGIN=APP_ID:/abs/key.pem for a "
                              "GitHub App reader)")
        self.assertEqual(credentials.reader_app_alternative(),
                         "--app <that login>=APP_ID:/abs/key.pem for a GitHub App reader")


if __name__ == "__main__":
    unittest.main()
