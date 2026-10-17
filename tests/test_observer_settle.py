"""`hermes dk observer --settle` (#724): an uncertain notice can be settled by a person."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import pathlib
import tempfile
import time
import unittest
from argparse import Namespace
from unittest.mock import patch

from diaktoros import observer
from diaktoros.cli import operations
from diaktoros.state import LoopState

import _ledger_guard  # noqa: E402

setUpModule, tearDownModule = _ledger_guard.module_home()

KEY = "private:157:abc:fixed:-"


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.loop = {"id": "private", "repo": "owner/private", "state_dir": self.temp.name,
                     "host": "", "observer": {"route": "private-observe", "profile": "default",
                                               "deliver": "telegram"}}
        self.state = LoopState(self.loop)
        old = time.time() - 10 * 86400
        self.write({KEY: {"status": "uncertain", "event": "fixed", "number": 157, "at": old,
                          "delivery": "delivery-1", "message": "fixed #157", "attempts": 1,
                          "error": "POST result unverified"},
                    "k:delivered": {"status": "delivered", "event": "opened", "number": 1,
                                    "at": old},
                    "k:pending": {"status": "pending", "event": "opened", "number": 2,
                                  "at": time.time()}})

    def write(self, entries):
        pathlib.Path(self.state.observations).write_text(json.dumps({"entries": entries}))

    def entry(self, key=KEY):
        return json.loads(pathlib.Path(self.state.observations).read_text())["entries"][key]

    def run_cmd(self, **kw):
        args = Namespace(**{"loop": "private", "list": False, "settle": None,
                            "delivered": False, "not_delivered": False, **kw})
        out = io.StringIO()
        with patch.object(operations.config, "load_id", return_value=self.loop), \
                patch.object(operations.state_mod, "state_for", return_value=self.state), \
                contextlib.redirect_stdout(out):
            rc = operations.cmd_observer(args)
        return rc, out.getvalue()

    def test_delivered_records_receipt_and_no_longer_owed(self):
        self.assertEqual(observer.unsettled(self.state), 2)
        rc, out = self.run_cmd(settle=KEY, delivered=True)
        self.assertEqual(rc, 0, out)
        entry = self.entry()
        self.assertEqual((entry["status"], entry["settled_by"]), ("delivered", "operator"))
        self.assertIn("settled_at", entry)
        self.assertEqual(observer.unsettled(self.state), 1)  # only the pending one remains
        self.assertNotIn(KEY, dict(observer.owed_entries(self.state)))

    def test_not_delivered_is_sent_once_by_the_sweep_with_original_id(self):
        rc, out = self.run_cmd(settle=KEY, not_delivered=True)
        self.assertEqual(rc, 0, out)
        sent = []

        def post(loop, block, tag, delivery, urgent=False):
            sent.append((block["message"], delivery))
            return True, "", False

        with patch.object(observer, "configured", return_value=""), \
                patch.object(observer, "_post", side_effect=post):
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            self.assertEqual(observer.retry(self.loop, self.state), 0)
        self.assertEqual(sent, [("fixed #157", "delivery-1")])
        self.assertEqual(self.entry()["status"], "delivered")

    def test_not_delivered_gets_only_one_retry(self):
        self.run_cmd(settle=KEY, not_delivered=True)
        calls = []
        with patch.object(observer, "configured", return_value=""), \
                patch.object(observer, "_post",
                             side_effect=lambda *a, **k: calls.append(1) or (False, "down", False)):
            observer.retry(self.loop, self.state)
            observer.retry(self.loop, self.state)
        self.assertEqual(len(calls), 1)

    def test_refuses_delivered_pending_and_unknown(self):
        for key in ("k:delivered", "k:pending"):
            rc, out = self.run_cmd(settle=key, delivered=True)
            self.assertEqual(rc, 2)
            self.assertIn(key, out)
        self.assertEqual(self.entry("k:pending")["status"], "pending")
        rc, out = self.run_cmd(settle="nope", delivered=True)
        self.assertEqual(rc, 2)
        self.assertIn(KEY, out)  # names the owed keys

    def test_needs_exactly_one_verdict(self):
        self.assertEqual(self.run_cmd(settle=KEY)[0], 2)
        self.assertEqual(self.run_cmd(settle=KEY, delivered=True, not_delivered=True)[0], 2)
        self.assertEqual(self.entry()["status"], "uncertain")

    def test_status_check_is_enforced_by_settle(self):
        # Without the 'uncertain' check a delivered entry would be rewritten and re-sent.
        with self.assertRaises(observer.SettleError):
            observer.settle(self.state, "k:delivered", delivered=False)
        self.assertEqual(self.entry("k:delivered")["status"], "delivered")

    def test_status_lists_uncertain_entry_and_the_settle_command(self):
        # Drives cmd_status itself, so dropping the _print_uncertain call turns this red.
        from diaktoros import config
        full = config.normalize({
            "id": "private", "repo": "owner/private", "host": "https://gw.example",
            "state_dir": self.temp.name, "fixers": ["fix"], "reviewers": ["review"],
            "tokens": {}, "read_token": "read",
            "seats": {"reviewer": {"route": "r", "profile": "critic", "login": "review"},
                      "fixer": {"route": "f", "profile": "coder", "login": "fix"}},
            "observer": {"route": "private-observe", "profile": "default",
                         "deliver": "telegram"}})
        out = io.StringIO()
        with patch.object(operations.config, "load_id", return_value=full), \
                patch.object(operations.state_mod, "state_for", return_value=self.state), \
                patch.object(operations, "_print_ledger_runs"), \
                contextlib.redirect_stdout(out):
            operations.cmd_status(Namespace(loop="private", seat=None, json=False))
        text = out.getvalue()
        for want in (KEY, "fixed #157", "10d 0h old", "POST result unverified",
                     "hermes dk observer --loop private --settle KEY"):
            self.assertIn(want, text)

    def test_list_shows_uncertain(self):
        rc, out = self.run_cmd(list=True)
        self.assertEqual(rc, 0)
        self.assertIn(KEY, out)
        self.assertIn("fixed #157", out)


if __name__ == "__main__":
    unittest.main()
