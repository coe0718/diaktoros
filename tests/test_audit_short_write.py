"""A short write to broker-audit.jsonl fails the operation and leaves no truncated line (#826)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, safe_push


def short_write(real_write=os.write):
    """An os.write that transfers only half of what it was given."""
    def write(fd, data):
        return real_write(fd, bytes(data)[:max(1, len(data) // 2)])
    return write


class AuditShortWriteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.state = pathlib.Path(temp.name) / "state"
        self.state.mkdir()
        self.loop = {"state_dir": str(self.state)}
        self.audit = self.state / "broker-audit.jsonl"

    def lines(self):
        return [json.loads(x) for x in self.audit.read_text().splitlines()]

    def test_broker_audit_fails_on_a_short_write_and_leaves_no_partial_line(self):
        broker._audit(self.loop, "o/r", 1, "a" * 40, "b", "reviewer", "review", "login")
        with mock.patch.object(broker.os, "write", short_write()):
            with self.assertRaisesRegex(OSError, "short audit write"):
                broker._audit(self.loop, "o/r", 2, "a" * 40, "b", "reviewer", "review", "login")
        self.assertEqual([r["pr"] for r in self.lines()], [1])  # every line still parses

    def test_broker_operation_reports_failure_when_the_audit_write_is_short(self):
        with mock.patch.object(broker, "_write", return_value=({"id": 5}, "")), \
                mock.patch.object(broker, "_signed", return_value="body"), \
                mock.patch.object(broker.os, "write", short_write()):
            with self.assertRaisesRegex(OSError, "short audit write"):
                broker.post_issue_comment(self.loop, repo="o/r", number=3, login="fixer",
                                          body="could not fix")
        self.assertEqual(self.audit.read_text(), "")

    def test_safe_push_audit_shares_the_rule_and_cuts_the_partial_line(self):
        safe_push._audit(self.loop, {"operation": "push", "n": 1})
        with mock.patch.object(broker.os, "write", short_write()):
            with self.assertRaisesRegex(OSError, "short audit write"):
                safe_push._audit(self.loop, {"operation": "push", "n": 2})
        self.assertEqual([r["n"] for r in self.lines()], [1])


if __name__ == "__main__":
    unittest.main()
