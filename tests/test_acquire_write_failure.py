"""#726: a seat claim that did not reach disk is not a claim.

``LoopState._save`` logs and swallows a write failure; ``acquire`` used to go on to record the
claim in the process-global ``CLAIMS`` and return, so the gate ran its turn believing it held a
seat no second gate could see. ``acquire`` now raises and ``gate.take_seat`` (the enforcing entry
point) refuses to start the run.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import gate, state  # noqa: E402

HEAD = "a" * 40
KEY = "acme/widgets#7"


class AcquireWriteFailure(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                     "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"],
                     "reviewers": ["r"], "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(self.loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)
        before = list(state.CLAIMS)
        self.addCleanup(lambda: state.CLAIMS.__setitem__(slice(None), before))

    def test_acquire_raises_and_records_no_claim_when_the_write_fails(self):
        before = len(state.CLAIMS)
        with mock.patch.object(state, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.st.acquire("reviewer", KEY, HEAD, "why")
        self.assertEqual(len(state.CLAIMS), before)
        self.assertNotIn(KEY, self.st.active("reviewer"))

    def test_acquire_still_claims_when_the_write_lands(self):
        self.st.acquire("reviewer", KEY, HEAD, "why")
        self.assertIn(KEY, self.st.active("reviewer"))

    def test_take_seat_does_not_run_when_the_claim_cannot_be_written(self):
        with mock.patch.object(state, "_atomic_write", side_effect=OSError("disk full")), \
                mock.patch.object(gate.isolation, "ensure") as ensure:
            with self.assertRaises(SystemExit):
                gate.take_seat(self.loop, self.st, "reviewer", 7, HEAD, "why")
        ensure.assert_not_called()          # no workspace was built for a run nobody can see
        self.assertNotIn(KEY, self.st.active("reviewer"))


if __name__ == "__main__":
    unittest.main()
