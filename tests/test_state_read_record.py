"""``state.read_record``: the one lock-free read of a state-dir record (#1485).

``conflict_person`` and ``escalate_pending`` each kept a copy of it; both now call this one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

from diaktoros import conflict_person, escalate_pending, state  # noqa: E402


class ReadRecord(unittest.TestCase):
    def setUp(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.st = types.SimpleNamespace(dir=root / "state")

    def test_absent_is_empty_and_silent(self):
        said = []
        self.assertEqual(state.read_record(self.st, "x.json", said.append), {})
        self.assertEqual(said, [])

    def test_unreadable_or_not_an_object_is_empty_and_said(self):
        self.st.dir.mkdir()
        said = []
        (self.st.dir / "x.json").write_text("{")
        self.assertEqual(state.read_record(self.st, "x.json", said.append), {})
        (self.st.dir / "x.json").write_text("[1]")
        self.assertEqual(state.read_record(self.st, "x.json", said.append), {})
        self.assertEqual(len(said), 2)
        self.assertIn("JSONDecodeError", said[0])
        self.assertIn("does not hold a JSON object", said[1])
        self.assertEqual(state.read_record(self.st, "x.json"), {})   # no callback: silent

    def test_an_object_is_returned(self):
        self.st.dir.mkdir()
        (self.st.dir / "x.json").write_text('{"7": {"at": 1}}')
        self.assertEqual(state.read_record(self.st, "x.json"), {"7": {"at": 1}})

    def test_both_stores_read_through_it(self):
        for module in (conflict_person, escalate_pending):
            with self.subTest(module=module.__name__), \
                    mock.patch.object(state, "read_record", return_value={"sentinel": 1}) as read:
                self.assertEqual(module._read(self.st), {"sentinel": 1})
                self.assertEqual(read.call_args.args[:2], (self.st, module.FILE))


if __name__ == "__main__":
    unittest.main()
