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


class StrictMutations(unittest.TestCase):
    """#726 (review F1): mutations whose callers act on the write must not swallow its failure."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                     "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"],
                     "reviewers": ["r"], "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(self.loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)

    def _all_raise(self, calls):
        for name, call in calls.items():
            with self.subTest(name), \
                    mock.patch.object(state, "_atomic_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    call()

    def test_every_recording_mutation_raises_when_the_write_fails(self):
        st = self.st
        self._all_raise({
            "inflight record": lambda: st.inflight(f"review:7:{HEAD}", record=True),
            "quarantine": lambda: st.quarantine(7, HEAD),
            "human_hold_set": lambda: st.human_hold_set(7, HEAD, "why"),
            "pr_hold_set": lambda: st.pr_hold_set(7, {"by": "m"}),
            "scope_hold_set": lambda: st.scope_hold_set(7, {"head": HEAD}),
            "fix_hold_set": lambda: st.fix_hold_set(8, 7),
            "fix_ask_set": lambda: st.fix_ask_set(8, "plan", 1, "text"),
            "issue_dep_add": lambda: st.issue_dep_add(8, 9, {"by": "o"}),
            "dep_wait_set": lambda: st.dep_wait_set(8),
            "review_cap_notice_once": lambda: st.review_cap_notice_once(7, HEAD),
            "review_cap_grant": lambda: st.review_cap_grant(7, HEAD, 3),
            "findings_put": lambda: st.findings_put(7, {"head": HEAD}),
        })

    def test_every_dropping_mutation_raises_when_the_write_fails(self):
        st = self.st
        st.pr_hold_set(7, {"by": "m"})
        st.scope_hold_set(7, {"head": HEAD})
        st.fix_hold_set(8, 7)
        st.issue_dep_add(8, 9, {"by": "o"})
        st.dep_wait_set(8)
        self._all_raise({
            "pr_hold_drop": lambda: st.pr_hold_drop(7),
            "scope_hold_drop": lambda: st.scope_hold_drop(7),
            "fix_hold_drop": lambda: st.fix_hold_drop(8),
            "issue_dep_drop": lambda: st.issue_dep_drop(8, 9),
            "dep_wait_drop": lambda: st.dep_wait_drop(8),
        })

    def test_releases_stay_best_effort(self):
        """A release that cannot be written is logged; the claim's TTL is the backstop."""
        self.st.acquire("reviewer", KEY, HEAD, "why")
        with mock.patch.object(state, "_atomic_write", side_effect=OSError("disk full")):
            self.st.release_if("reviewer", KEY)
            self.st.inflight_clear(f"review:7:{HEAD}")

    def test_claim_seat_keeps_its_claim_releasable_when_the_mark_cannot_be_written(self):
        from diaktoros.run_supervisor import claim_seat
        row = {"seat": "reviewer", "pr": 7, "head": HEAD, "id": "run-1"}
        before = list(state.CLAIMS)
        self.addCleanup(lambda: state.CLAIMS.__setitem__(slice(None), before))
        real = state.LoopState._save_strict

        def fail_inflight(this, path, data):
            if path == this.inflight_file:
                raise OSError("disk full")
            return real(this, path, data)

        with mock.patch.object(state, "state_for", return_value=self.st), \
                mock.patch.object(state.LoopState, "_save_strict", fail_inflight):
            claim = claim_seat(self.loop, row, 900)
        self.assertIsNotNone(claim)                         # the claim itself is on disk
        self.assertIsNone(claim[4])                         # no mark was written, none to clear
        self.assertIn(KEY, self.st.active("reviewer"))


if __name__ == "__main__":
    unittest.main()
