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
        self.assertEqual(self.kind_of("changes requested at head aaaaaaa (1 of 3 verdicts)",
                                      reviews=[review("CHANGES_REQUESTED")]), ready.FAILURE)
        # #1245: the waiting text and the verdict text differ, so a red check says why.
        self.assertFalse(any("not approved" in text
                             for _, text in self.kinds(reviews=[review("CHANGES_REQUESTED")])))

    def test_red_ci_is_a_failure_and_running_ci_is_pending(self):
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(failed=["tests"])),
                         ready.FAILURE)
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(cancelled=["tests"])),
                         ready.FAILURE)
        self.assertEqual(self.kind_of("required checks", ci_state=ci.CIState(pending=["tests"])),
                         ready.PENDING)
        # A required check that never reported is for the operator, not a wait that ends — once
        # the head has been seen long enough for its workflow to have started (shadow, #1225).
        self.assertEqual(self.kind_of("never reported", ci_state=ci.CIState(passed=["lint"])),
                         ready.PENDING)                         # head not seen yet: starting
        import time
        self.st.watch_save({"heads": {"7": {"sha": HEAD, "observed_at": time.time() - 60}}})
        self.assertEqual(self.kind_of("never reported", ci_state=ci.CIState(passed=["lint"])),
                         ready.PENDING)                         # a minute in: still starting
        self.st.watch_save({"heads": {"7": {"sha": HEAD,
                                            "observed_at": time.time() - ready.MISSING_GRACE_S - 1}}})
        self.assertEqual(self.kind_of("never reported", ci_state=ci.CIState(passed=["lint"])),
                         ready.FAILURE)
        self.st.watch_save({"heads": {"7": {"sha": "b" * 40, "observed_at": 1.0}}})
        self.assertEqual(self.kind_of("never reported", ci_state=ci.CIState(passed=["lint"])),
                         ready.PENDING)                         # an older head's record: not this one

    def test_a_head_open_at_arming_ages_its_grace_from_armed_since(self):
        # #1232: the watchdog records no observed_at for a head it found at arming.
        import time
        for armed, kind in ((time.time() - ready.MISSING_GRACE_S - 1, ready.FAILURE),
                            (time.time() - 60, ready.PENDING), (None, ready.PENDING),
                            (True, ready.PENDING)):
            with self.subTest(armed=armed):
                self.st.watch_save({"armed_since": armed,
                                    "heads": {"7": {"sha": HEAD, "observed_at": None}}})
                self.assertEqual(self.kind_of("never reported",
                                              ci_state=ci.CIState(passed=["lint"])), kind)

    def test_the_grace_runs_from_the_first_sighting_and_ignores_bad_clocks(self):
        # #1236: a draft opened before arming and marked ready later is new to the loop then;
        # #1235: a corrupt or future clock never ends the grace early.
        import time
        now, old = time.time(), time.time() - ready.MISSING_GRACE_S - 1
        for entry, armed, kind in (
                ({"first_seen_at": now - 60}, old, ready.PENDING),         # just seen: starting
                ({"first_seen_at": old}, now - 60, ready.FAILURE),
                ({"first_seen_at": now + 3600}, old, ready.FAILURE),       # bad: falls back
                ({"observed_at": float("inf"), "first_seen_at": now - 60}, old, ready.PENDING),
                ({}, now + 3600, ready.PENDING), ({}, float("nan"), ready.PENDING),
                ({}, -5, ready.PENDING)):
            with self.subTest(entry=entry, armed=armed):
                self.st.watch_save({"armed_since": armed, "heads": {"7": {
                    "sha": HEAD, "observed_at": None, **entry}}})
                self.assertEqual(self.kind_of("never reported",
                                              ci_state=ci.CIState(passed=["lint"])), kind)

    def test_the_grace_reads_the_watch_record_once(self):
        # #1231
        with mock.patch.object(self.st, "watch", wraps=self.st.watch) as watch:
            ready._past_start_grace(self.st, 7, HEAD)
        self.assertEqual(watch.call_count, 1)

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
            # No adjudicator route: no ruling ever comes, so a person decides (shadow, #1225).
            self.assertEqual(self.kind_of("no adjudicator route: a person decides"),
                             ready.FAILURE)
            self.loop["adjudicator"] = {"route": "w-breach"}
            self.assertEqual(self.kind_of("escalated: parked awaiting adjudication"),
                             ready.PENDING)
        with mock.patch.object(gate, "_ruled", return_value="REJECT"):
            # #1245: once ruled, the reason names the ruling, never "awaiting" (live trial #18).
            self.assertEqual(self.kind_of("the adjudicator ruled REJECT at head aaaaaaa"),
                             ready.FAILURE)
            self.assertFalse(any("awaiting" in text for _, text in self.kinds()))

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


class Accessor(unittest.TestCase):
    def test_auto_follows_the_reviewer_identity_and_on_off_override_it(self):
        from diaktoros import config
        pat = {"reviewer_seat": "rev", "tokens": {"rev": "/t"}}
        app = {"reviewer_seat": "r[bot]", "apps": {"r[bot]": {"app_id": 1, "key_file": "/k"}}}
        for loop, mode, on in ((pat, None, False), (pat, "auto", False), (pat, "on", True),
                               (app, None, True), (app, "auto", True), (app, "off", False),
                               (pat, "off", False)):
            with self.subTest(loop=loop["reviewer_seat"], mode=mode):
                self.assertIs(config.merge_check_on({**loop, "merge_check": mode}), on)


class Posting(unittest.TestCase):
    """``post`` and ``refresh`` against a faked ``gh.api`` and ``ready.collect_kinds``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "reviewer_seat": "r[bot]",
                     "apps": {"r[bot]": {"app_id": 1, "key_file": "/k"}},
                     "seats": {"reviewer": {"login": "r[bot]"}},
                     "state_dir": str(pathlib.Path(tmp.name) / "state")}
        self.st = state_mod.state_for(self.loop)
        self.calls, self.fail = [], False
        self.prs = [{"number": 7, "head": {"sha": HEAD}}]
        self.kinds = {7: [(ready.PENDING, "review not yet posted")]}

        def api(loop, path, method="GET", body=None, login=None):
            self.calls.append((path, method, body, login))
            if self.fail:
                return None
            if "/check-runs" in path:
                return {"id": int(path.rsplit("/", 1)[1]) if method == "PATCH" else 55}
            return {"context": body["context"]}
        for patch in (mock.patch.object(gh, "api", side_effect=api),
                      mock.patch.object(gh, "last_error", return_value="HTTP 403"),
                      mock.patch.object(ready, "collect_kinds",
                                        side_effect=lambda *a: (self.prs, self.kinds, ""))):
            patch.start()
            self.addCleanup(patch.stop)

    def refresh(self, **kw):
        return merge_check.refresh(self.loop, self.st, **kw)

    def memory(self):
        return self.st._load(self.st.dir / merge_check.FILE, {})

    def test_an_app_reviewer_posts_a_check_run_then_updates_it(self):
        self.assertEqual(self.refresh(), 1)
        [(path, method, body, login)] = self.calls
        self.assertEqual((path, method, login), ("/repos/acme/widgets/check-runs", "POST", "r[bot]"))
        self.assertEqual((body["name"], body["head_sha"], body["status"]),
                         ("diaktoros/review", HEAD, "in_progress"))
        self.assertNotIn("conclusion", body)
        self.assertEqual(self.memory()["7"]["run_id"], 55)
        self.kinds = {7: []}
        self.assertEqual(self.refresh(), 1)
        path, method, body, _ = self.calls[1]
        self.assertEqual((path, method), ("/repos/acme/widgets/check-runs/55", "PATCH"))
        self.assertEqual((body["status"], body["conclusion"], body["output"]["title"]),
                         ("completed", "success", "Ready to merge"))
        self.assertNotIn("head_sha", body)
        self.assertEqual(len(self.calls), 2)          # updated, not duplicated

    def test_a_new_head_gets_a_new_check_run(self):
        self.refresh()
        self.prs = [{"number": 7, "head": {"sha": "b" * 40}}]
        self.refresh()
        self.assertEqual([c[:2] for c in self.calls],
                         [("/repos/acme/widgets/check-runs", "POST")] * 2)
        self.assertEqual(self.calls[1][2]["head_sha"], "b" * 40)

    def test_a_failed_update_is_retried_and_only_a_vanished_run_is_replaced(self):
        # #1230: a PATCH that fails for any reason but 404 must not leave a second run on the head.
        self.refresh()
        self.kinds = {7: []}
        self.fail = True
        logged = []
        self.assertEqual(self.refresh(log=logged.append), 0)
        self.assertEqual([c[1] for c in self.calls], ["POST", "PATCH"])   # no second POST
        self.assertEqual(self.memory()["7"]["state"], "pending")         # not recorded: retried
        gh.last_error.return_value = "HTTP 404 Not Found"
        self.assertEqual(self.refresh(), 0)                             # still failing: the POST too
        self.assertEqual([c[1] for c in self.calls], ["POST", "PATCH", "PATCH", "POST"])
        self.fail = False
        self.calls.clear()
        with mock.patch.object(gh, "api", side_effect=lambda loop, path, method="GET", body=None,
                               login=None: self.calls.append((path, method)) or (
                                   None if method == "PATCH" else {"id": 56})):
            self.assertEqual(self.refresh(), 1)                         # the run is gone: a new one
        self.assertEqual([c[1] for c in self.calls], ["PATCH", "POST"])
        self.assertEqual(self.memory()["7"]["run_id"], 56)

    def test_an_unchanged_state_is_not_rewritten(self):
        self.assertEqual(self.refresh(), 1)
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(len(self.calls), 1)
        self.kinds = {7: [(ready.PENDING, "review not yet posted"), (ready.GITHUB, "blocked")]}
        self.assertEqual(self.refresh(), 0)           # GitHub's blocked does not count

    def test_a_pat_loop_that_opted_in_posts_a_commit_status(self):
        self.loop.update(reviewer_seat="rev", apps={}, tokens={"rev": "/t"}, merge_check="on",
                         seats={"reviewer": {"login": "rev"}})
        self.kinds = {7: [(ready.FAILURE, "changes requested at this head " + "x" * 300)]}
        self.assertEqual(self.refresh(), 1)
        [(path, method, body, login)] = self.calls
        self.assertEqual((path, method, login), (f"/repos/acme/widgets/statuses/{HEAD}", "POST", "rev"))
        self.assertEqual((body["state"], body["context"]), ("failure", "diaktoros/review"))
        self.assertTrue(body["description"].startswith(
            "Not mergeable: changes requested at this head xxx"), body["description"])
        self.assertLessEqual(len(body["description"]), merge_check.STATUS_DESCRIPTION_MAX)
        self.assertNotIn("run_id", self.memory()["7"])

    def test_off_reads_and_writes_nothing(self):
        for loop in ({**self.loop, "merge_check": "off"},
                     {**self.loop, "reviewer_seat": "rev", "apps": {}, "tokens": {"rev": "/t"}}):
            with self.subTest(loop.get("merge_check")):
                self.assertEqual(merge_check.refresh(loop, self.st), 0)
        ready.collect_kinds.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_a_pr_outside_the_loop_gets_no_check(self):
        self.kinds = {7: [(ready.OUTSIDE, "not opened by the loop's fixer")]}
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(self.calls, [])

    def test_an_unreadable_listing_writes_nothing(self):
        ready.collect_kinds.side_effect = lambda *a: ([], {}, "open PRs unreadable")
        logged = []
        self.assertEqual(self.refresh(log=logged.append), 0)
        self.assertEqual(self.calls, [])
        self.assertIn("open PRs unreadable", logged[0])

    def test_a_failed_write_is_not_recorded_and_is_retried(self):
        self.fail = True
        logged = []
        self.assertEqual(self.refresh(log=logged.append), 0)
        self.assertEqual(self.memory(), {})
        self.assertIn("HTTP 403", logged[0])
        self.fail = False
        self.assertEqual(self.refresh(), 1)

    def test_only_limits_the_prs_and_closed_prs_are_pruned(self):
        self.prs.append({"number": 8, "head": {"sha": "c" * 40}})
        self.kinds[8] = []
        self.assertEqual(self.refresh(only={8}), 1)
        self.assertEqual(list(self.memory()), ["8"])
        self.prs = [{"number": 7, "head": {"sha": HEAD}}]
        self.assertEqual(self.refresh(), 1)
        self.assertEqual(list(self.memory()), ["7"])

    def test_mark_pending_posts_once_per_head_and_refresh_updates_the_same_run(self):
        # #1242 (live trial): a new PR showed no check until the first refresh.
        self.assertTrue(merge_check.mark_pending(self.loop, self.st, 7, HEAD, "review queued"))
        [(path, method, body, _)] = self.calls
        self.assertEqual((path, method, body["status"]), ("/repos/acme/widgets/check-runs",
                                                          "POST", "in_progress"))
        self.assertIn("review queued", body["output"]["summary"])
        self.assertNotIn("nothing needs a change", body["output"]["summary"])   # shadow, #1249
        self.assertFalse(merge_check.mark_pending(self.loop, self.st, 7, HEAD, "again"))
        self.assertEqual(len(self.calls), 1)                          # once per head
        self.kinds = {7: []}
        self.refresh()
        self.assertEqual(self.calls[1][:2], ("/repos/acme/widgets/check-runs/55", "PATCH"))

    def test_mark_pending_is_off_with_the_check_and_records_no_failed_write(self):
        self.assertFalse(merge_check.mark_pending({**self.loop, "merge_check": "off"}, self.st,
                                                  7, HEAD, "x"))
        self.assertEqual(self.calls, [])
        self.fail = True
        self.assertFalse(merge_check.mark_pending(self.loop, self.st, 7, HEAD, "x"))
        self.assertEqual(self.memory(), {})
        for error in (RuntimeError("boom"), gh.GateBudgetExceeded("budget spent")):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(merge_check, "post", side_effect=error):
                self.assertFalse(merge_check.mark_pending(self.loop, self.st, 7, HEAD, "x"))

    def test_a_spent_gate_budget_in_the_pending_mark_still_ends_the_gate_accepted(self):
        # Shadow review of #1249: GateBudgetExceeded is a BaseException, so it escaped the gate
        # after the turn was armed, as a gate failure with no recorded decision.
        from diaktoros import util
        st, recorded = mock.Mock(), []
        st.queue_items.return_value = {}
        with mock.patch.object(gate, "enqueue_isolated", return_value="enqueued"), \
                mock.patch.object(merge_check, "post",
                                  side_effect=gh.GateBudgetExceeded("budget spent")), \
                mock.patch.object(util, "_DECISION_RECORDER",
                                  lambda reason, decision: recorded.append((decision, reason))), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO), \
                self.assertRaises(SystemExit) as caught:
            gate.block_pr_agent(self.loop, self.st, "reviewer", 7, HEAD)
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(recorded, [("accepted", "reviewer turn enqueued")])

    def test_the_gate_marks_pending_only_for_a_scheduled_review(self):
        st = mock.Mock()
        st.queue_items.return_value = {}
        for seat, outcome, called in (("reviewer", "enqueued", True),
                                      ("reviewer", "duplicate succeeded", False),
                                      ("fixer", "enqueued", False)):
            with self.subTest(seat=seat, outcome=outcome), \
                    mock.patch.object(gate, "enqueue_isolated", return_value=outcome), \
                    mock.patch.object(merge_check, "mark_pending") as mark, \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO), \
                    self.assertRaises(SystemExit):
                loop = {**self.loop, "unattended_fixer_push": True}
                try:
                    gate.block_pr_agent(loop, st, seat, 7, HEAD)
                finally:
                    self.assertEqual(mark.called, called)

    def test_refresh_quietly_never_raises(self):
        from diaktoros import util
        with mock.patch.object(merge_check, "refresh", side_effect=RuntimeError("boom")), \
                mock.patch.object(util, "log") as log:
            merge_check.refresh_quietly(self.loop, 7)
        self.assertIn("boom", log.call_args.args[0])
        with mock.patch.object(merge_check, "refresh") as refresh:
            merge_check.refresh_quietly(self.loop, 7)
        self.assertEqual(refresh.call_args.kwargs["only"], {7})


if __name__ == "__main__":
    unittest.main()
