"""#2148: ``retry --clear-hold`` says what it reset when the hold itself had already lapsed.

A lapsed outage hold leaves its strikes and the provider-failure count behind, and they matter: a
leftover strike doubles the next outage window, and a count at the limit holds the next failure
at once. ``clear_holds`` dropped them silently and the command printed "nothing to clear". It now
returns them apart from the live holds, and the command names each account it reset.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import pacing, retry_action  # noqa: E402
from diaktoros.cli import operations  # noqa: E402

ERROR = "HTTP 503: upstream connect error"


class LapsedHold(unittest.TestCase):
    def setUp(self):
        self.key = pacing.account_key("deepseek", "https://api.deepseek.com", "fix")
        self.addCleanup(lambda: pacing.path().unlink(missing_ok=True))
        pacing.path().unlink(missing_ok=True)
        # A provider failure at the limit an hour ago: held, then the hold lapsed, with its
        # strike and the count left behind.
        then = time.time() - 3600
        pacing.count_provider_failure(self.key, ERROR, 1, now=then)
        self.assertIsNone(pacing.held(self.key))

    def stored(self, section):
        return json.loads(pacing.path().read_text()).get(section) or {}

    def test_clear_holds_returns_the_accounts_it_reset_apart_from_live_holds(self):
        live = pacing.account_key("anthropic", "https://api.anthropic.com", "fix")
        pacing.hold(live, time.time() + 3600, "anthropic answered 429")
        self.assertIn(self.key, self.stored("unavailable"))
        self.assertIn(self.key, self.stored("provider_failures"))
        self.assertEqual(pacing.clear_holds({"fix"}), ([live], [self.key]))
        for section in ("holds", "unavailable", "provider_failures"):
            self.assertEqual(self.stored(section), {}, section)
        self.assertEqual(pacing.clear_holds({"fix"}), ([], []))       # nothing left to reset

    def test_the_command_names_the_reset_instead_of_nothing_to_clear(self):
        loop = {"id": "widgets"}
        args = argparse.Namespace(seat="fixer")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(retry_action.config, "seat_profile", return_value="fix"), \
                mock.patch.object(operations, "_ledger_path",
                                  return_value=pathlib.Path(tmp) / "none.sqlite"), \
                redirect_stdout(out):
            self.assertFalse(operations._retry_clear_hold(args, loop))
        said = out.getvalue()
        self.assertIn("[widgets] no live hold; its counts reset: deepseek as profile fix", said)
        self.assertNotIn("nothing to clear", said)
        self.assertEqual(self.stored("unavailable"), {})


if __name__ == "__main__":
    unittest.main()
