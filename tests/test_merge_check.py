"""The ``diaktoros/review`` merge check's state (#1163 M2b-1).

``ready.assess_kinds`` gives every reason ``ready`` excludes a head for a kind (pending, failure,
GitHub's own merge verdict, or not a loop PR); ``merge_check.decide`` turns them into one state.
GitHub's ``blocked`` never counts (it would make the required check wait on itself), and the
loop's CI reader skips the check so it is never one of the head's own checks.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import ci, gate, gh, merge_check, ready, state as state_mod  # noqa: E402

HEAD = "a" * 40
CLEAN = {"mergeable": True, "mergeable_state": "clean", "number": 7, "state": "open",
         "draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}}


def review(state):
    return {"id": 1, "state": state, "commit_id": HEAD, "submitted_at": "2026-01-01T00:00:00Z",
            "user": {"login": "rev", "id": 7}}


class KindsBase(unittest.TestCase):
    """The fixture only (no tests), shared by Kinds and Stacked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "read_token": "rev", "reviewers": ["rev"], "reviewer_seat": "rev",
                     "fixers": ["fix"], "review_only": [], "required_checks": ["tests"],
                     "state_dir": str(pathlib.Path(tmp.name) / "state"), "cooldown_h": 6,
                     "seats": {"reviewer": {"route": "w-review"}, "fixer": {"route": "w-fix"}}}
        self.st = state_mod.state_for(self.loop)
        self.pr = {"number": 7, "draft": False, "user": {"login": "fix"},
                   "base": {"ref": "main"}, "head": {"sha": HEAD, "ref": "b7"}}
        for patch in (mock.patch.object(ready.gate_failures, "open_for", return_value=[]),
                      mock.patch.object(gate, "shadow_state", return_value=("", "", ""))):
            patch.start()
            self.addCleanup(patch.stop)

    def kinds(self, reviews=None, ci_state=None, detail=None, **pr):
        return ready.assess_kinds(self.loop, self.st, {**self.pr, **pr},
                                  [review("APPROVED")] if reviews is None else reviews,
                                  ci_state or ci.CIState(passed=["tests"]),
                                  CLEAN if detail is None else detail)

    def kind_of(self, needle, **kw):
        found = [kind for kind, text in self.kinds(**kw) if needle in text]
        self.assertEqual(len(found), 1, self.kinds(**kw))
        return found[0]


class Kinds(KindsBase):
    def test_a_clean_approved_head_has_no_reasons_and_assess_matches(self):
        self.assertEqual(self.kinds(), [])
        self.assertEqual(ready.assess(self.loop, self.st, self.pr, [review("APPROVED")],
                                      ci.CIState(passed=["tests"]), CLEAN), [])

    def test_no_verdict_yet_is_pending_and_changes_requested_is_a_failure(self):
        self.assertEqual(self.kind_of("not approved", reviews=[]), ready.PENDING)
        self.assertEqual(self.kind_of("not approved", reviews=[review("CHANGES_REQUESTED")]),
                         ready.FAILURE)

    def test_red_ci_is_a_failure_and_running_ci_is_pending(self):
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(failed=["tests"])),
                         ready.FAILURE)
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(cancelled=["tests"])),
                         ready.FAILURE)
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(pending=["tests"])),
                         ready.PENDING)
        # A required check that never reported is for the operator, not a wait that ends.
        self.assertEqual(self.kind_of("never reported", ci_state=ci.CIState(passed=["lint"])),
                         ready.FAILURE)

    def test_a_conflict_fails_and_githubs_other_verdicts_are_set_apart(self):
        self.assertEqual(self.kind_of("conflicts", detail={**CLEAN, "mergeable": False,
                                                           "mergeable_state": "dirty"}),
                         ready.FAILURE)
        self.assertEqual(self.kind_of("GitHub reports blocked",
                                      detail={**CLEAN, "mergeable_state": "blocked"}), ready.GITHUB)
        # Shadow review of #1225: only blocked is GitHub reflecting the check back.
        self.assertEqual(self.kind_of("GitHub reports behind",
                                      detail={**CLEAN, "mergeable_state": "behind"}), ready.FAILURE)
        for mstate in ("unknown", None):
            with self.subTest(mstate=mstate):
                self.assertEqual(self.kind_of("GitHub reports", detail={**CLEAN,
                                                                        "mergeable_state": mstate}),
                                 ready.PENDING)

    def test_holds_and_failures_need_a_person(self):
        self.st.human_hold_set(7, HEAD, "touches human_paths")
        self.assertEqual(self.kind_of("waiting for a person"), ready.FAILURE)

    def test_a_stale_approval_is_a_failure(self):
        self.st.watch_save({"stale_approvals": {"7": {"head": HEAD, "why": "tests is red"}}})
        self.assertEqual(self.kind_of("stale"), ready.FAILURE)

    def test_an_escalation_waits_for_its_ruling_then_the_operator_decides(self):
        self.st.breach.parent.mkdir(parents=True, exist_ok=True)
        state_mod._atomic_write(self.st.breach, {"acme/widgets#7": {
            "pr": 7, "head": HEAD, "rounds": 3, "cap": 3, "reason": "r", "at": "now",
            "status": "awaiting-adjudication"}})
        with mock.patch.object(gate, "_ruled", return_value=None):
            self.assertEqual(self.kind_of("escalated"), ready.PENDING)
        with mock.patch.object(gate, "_ruled", return_value="REJECT"):
            self.assertEqual(self.kind_of("escalated"), ready.FAILURE)

    def test_the_shadow_states_kind_is_carried(self):
        for kind in (ready.PENDING, ready.FAILURE):
            with self.subTest(kind=kind), \
                    mock.patch.object(gate, "shadow_state", return_value=(kind, "held", "")):
                self.assertEqual(self.kind_of("shadow review: held"), kind)

    def test_a_draft_is_pending_and_a_stranger_is_outside(self):
        self.assertEqual(self.kind_of("draft PR", draft=True,
                                      detail={**CLEAN, "draft": True}), ready.PENDING)
        self.assertEqual(self.kind_of("not one of this loop", user={"login": "stranger"}),
                         ready.OUTSIDE)


class Stacked(KindsBase):
    """Shadow review of #1225: build's own reasons reach the check (``build_kinds``)."""

    def test_a_stacked_pr_on_a_parent_that_is_not_ready_is_pending(self):
        parent = {**self.pr, "number": 1, "head": {"sha": "b" * 40, "ref": "b1"}}
        child = {**self.pr, "number": 2, "base": {"ref": "b1"}, "head": {"sha": HEAD, "ref": "b2"}}
        reads = {1: {"reviews": [], "ci": ci.CIState(passed=["tests"]),
                     "detail": {**CLEAN, "number": 1, "head": {"sha": "b" * 40}}, "files": ["a"]},
                 2: {"reviews": [{**review("APPROVED")}], "ci": ci.CIState(passed=["tests"]),
                     "detail": {**CLEAN, "number": 2, "base": {"ref": "b1"}}, "files": ["b"]}}
        out, kinds = ready.build_kinds(self.loop, self.st, [parent, child], reads)
        self.assertEqual(out, [])
        self.assertIn((ready.PENDING, "stacked on #1, which is not ready"), kinds[2])
        self.assertEqual(merge_check.decide(kinds[2])[0], merge_check.PENDING)
        _ready, excluded = ready.build(self.loop, self.st, [parent, child], reads)
        self.assertEqual({e["number"]: e["why"] for e in excluded}[2],
                         ["stacked on #1, which is not ready"])


class ShadowKinds(__import__("test_shadow_escalate_ruling").Base):
    """Shadow review of #1225 (F1): ``gate.shadow_state``'s kinds on a real run ledger."""

    def state(self):
        import test_fixer_gating as fg
        return gate.shadow_state(self.loop, 7, fg.HEAD, db=self.db, marker=self.st.breach_get(7))

    def test_a_shadow_hold_not_escalated_is_a_failure(self):
        self.shadow("REQUEST_CHANGES")
        self.assertEqual(self.state()[0], "failure")

    def test_escalated_and_waiting_is_pending_and_a_ruling_against_it_is_a_failure(self):
        self.shadow("REQUEST_CHANGES")
        self.mark_escalated()
        self.assertEqual(self.state()[0], "pending")
        self.ruled("REJECT")
        self.assertEqual(self.state()[0], "failure")

    def test_an_accept_or_an_approving_shadow_holds_nothing(self):
        self.shadow("REQUEST_CHANGES")
        self.mark_escalated()
        self.ruled("ACCEPT")
        self.assertEqual(self.state()[:2], ("", ""))

    def test_a_shadow_still_running_is_pending(self):
        import sqlite3
        import time
        from contextlib import closing
        import test_fixer_gating as fg
        self.shadow("APPROVE")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('later','dl',?,7,?,'shadow','running',0,'tl',?,?)",
                        (fg.REPO, fg.HEAD, time.time() + 100, time.time() + 100))
        self.assertEqual(self.state()[0], "pending")


class Decide(unittest.TestCase):
    def test_nothing_against_the_head_is_success(self):
        self.assertEqual(merge_check.decide([])[0], merge_check.SUCCESS)

    def test_githubs_blocked_alone_never_holds_the_check(self):
        # With the check required, GitHub says blocked *because* the check is not green yet.
        state, _ = merge_check.decide([(ready.GITHUB, "not mergeable: GitHub reports blocked")])
        self.assertEqual(state, merge_check.SUCCESS)

    def test_any_failure_fails_and_lists_the_rest_as_waiting(self):
        state, summary = merge_check.decide([(ready.PENDING, "CI unreadable"),
                                             (ready.FAILURE, "not approved at head aaaaaaa")])
        self.assertEqual(state, merge_check.FAILURE)
        self.assertLess(summary.index("not approved"), summary.index("(waiting) CI unreadable"))

    def test_only_pending_reasons_are_pending(self):
        state, summary = merge_check.decide([(ready.PENDING, "draft PR"),
                                             (ready.GITHUB, "GitHub reports unknown")])
        self.assertEqual(state, merge_check.PENDING)
        self.assertIn("draft PR", summary)
        self.assertNotIn("GitHub reports", summary)

    def test_a_stranger_gets_no_check(self):
        self.assertIsNone(merge_check.decide([(ready.OUTSIDE, "author x is not one")])[0])

    def test_titles(self):
        self.assertEqual(merge_check.title(merge_check.FAILURE), "Not mergeable")


class SelfExclusion(unittest.TestCase):
    """The loop's CI reader never counts its own check (it would gate on itself)."""

    def test_the_merge_check_is_skipped_as_a_check_run_and_as_a_status(self):
        def api(loop, path, login=None, **kw):
            if "/check-runs" in path:
                return {"total_count": 2, "check_runs": [
                    {"id": 1, "name": "tests", "status": "completed", "conclusion": "success"},
                    {"id": 2, "name": ci.MERGE_CHECK, "status": "completed",
                     "conclusion": "failure"}]}
            if path.split("?")[0].endswith("/status"):
                return {"state": "failure", "total_count": 1, "statuses": [
                    {"context": ci.MERGE_CHECK, "state": "failure"}]}
            raise AssertionError(path)
        loop = {"repo": "acme/widgets", "read_token": "rev"}
        with mock.patch.object(gh, "api", side_effect=api):
            state = ci.read(loop, HEAD)
        self.assertEqual((state.passed, state.failed, state.pending), (["tests"], [], []))
        self.assertTrue(ci.gating(state, ["tests"]).green)


if __name__ == "__main__":
    unittest.main()
