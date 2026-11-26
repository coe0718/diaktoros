"""The fixer -> reviewer handoff when the reviewer is a GitHub App (#1163 M2a).

GitHub cannot request an App bot as a reviewer: ``POST …/requested_reviewers`` answers 201 and
does nothing, and no ``review_requested`` is sent (docs/github-app-support.md, row 3). So when the
reviewer seat is an App, the fixer's request is authorized and audited as always, nothing is
sent to ``requested_reviewers``, and the host delivers the same ``review_requested`` event to the
reviewer gate (``review_kick``, as for #532), where every gate rule decides as for a real one.
An issue fix's PR skips the request (its ``opened`` wakes the gate). The watchdog's backstop and
``explain``'s next step say ``hermes dk review`` instead of a GitHub request.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_answers as fa  # noqa: E402
from diaktoros import broker, config, explain, gate, github_app, review_kick, state as state_mod  # noqa: E402
from diaktoros import routes, watch_mode  # noqa: E402
from diaktoros import merge_only  # noqa: E402


class Predicate(unittest.TestCase):
    def test_only_an_app_reviewer_seat_counts(self):
        app = {"reviewer_seat": "rev[bot]", "tokens": {"fix": "/f"}, "apps": {"rev[bot]": {}}}
        self.assertTrue(config.reviewer_is_app(app))
        self.assertTrue(config.reviewer_is_app({**app, "reviewer_seat": "REV[bot]"}))
        self.assertFalse(config.reviewer_is_app({**app, "apps": {}}))
        self.assertFalse(config.reviewer_is_app({**app, "reviewer_seat": "fix"}))
        self.assertFalse(config.reviewer_is_app({"reviewer_seat": "", "apps": {"": {}}}))
        # A PAT wins: a loop file holding both (which normalize refuses) never reads as an App.
        self.assertFalse(config.reviewer_is_app({**app, "tokens": {"rev[bot]": "/r"}}))

    def test_github_app_is_app_is_the_same_rule(self):
        """#1254: ``github_app.is_app`` and ``config.is_app`` answer one login alike, a login
        mapped as both a PAT and an App included (the PAT wins in both)."""
        both = {"tokens": {"Rev[bot]": "/r"}, "apps": {"rev[bot]": {"app_id": 1}}}
        apps_only = {"apps": both["apps"]}
        for loop, login in ((both, "rev[bot]"), (both, "REV[bot]"), (apps_only, "REV[bot]"),
                            (apps_only, ""), ({}, "rev[bot]")):
            self.assertEqual(github_app.is_app(loop, login), config.is_app(loop, login), (loop, login))
        self.assertFalse(github_app.is_app(both, "rev[bot]"))

    def test_hook_write_need_names_the_pat_remedy_for_a_pat_and_app_login(self):
        """A caller of ``github_app.is_app`` that does not check ``tokens`` first: a login mapped
        as both gets the PAT remedy, as ``config.reader_access_fix`` gives it (#1254)."""
        from diaktoros.cli import common
        loop = {"repo": "o/r", "read_token": "rev[bot]", "tokens": {"rev[bot]": "/r"},
                "apps": {"rev[bot]": {"app_id": 1}}}
        self.assertIn("the token for 'rev[bot]'", common.hook_write_need(loop, None))
        self.assertIn("give the read token", config.reader_access_fix(loop, "hooks"))


class AppSeat(fa.Base):
    """The real run broker, with the reviewer seat a GitHub App (its key mapped in ``apps``); its
    principal and token are the App path's (``github_app``), proven live in M1b."""

    def setUp(self):
        # #2743: no head in these fixtures is a merge-only one; nothing reads GitHub for it.
        off = mock.patch.object(merge_only, "lookup", return_value="")
        off.start()
        self.addCleanup(off.stop)
        super().setUp()
        key = self.root / "review.pem"
        key.write_text("k")
        key.chmod(0o600)
        del self.loop["tokens"]["review"]
        self.loop["apps"] = {"review": {"app_id": 2, "key_file": str(key)}}
        principal = mock.patch.object(github_app, "principal",
                                      return_value={"login": "review", "id": 2, "type": "Bot"})
        principal.start()
        self.addCleanup(principal.stop)
        minted = mock.patch.object(github_app, "installation_token", return_value="ghs_" + "x" * 386)
        minted.start()
        self.addCleanup(minted.stop)


class AppReviewer(AppSeat):

    def test_the_request_is_delivered_to_the_reviewer_gate_not_to_github(self):
        with mock.patch.object(review_kick, "kick", return_value=True) as kick:
            with self.serving() as server:
                self.assertTrue(self.send(server, {"operation": "push",
                                                   "manifest": fa.manifest()})["ok"])
                self.assertTrue(self.send(server, {"operation": "request_review"})["ok"])
        self.assertNotIn("requested_reviewers",
                         [p.rsplit("/", 1)[1] for p, _, _ in self.fake.posts()])
        kick.assert_called_once()
        _loop, pr, head, sender, _tag = kick.call_args.args
        self.assertEqual((pr["number"], head, sender), (7, fa.NEW_HEAD, "fix"))
        audit = (Path(self.loop["state_dir"]) / "broker-audit.jsonl").read_text()
        self.assertIn('"operation": "request_review"', audit)

    def test_answers_still_post_before_the_handoff(self):
        with mock.patch.object(review_kick, "kick", return_value=True) as kick:
            with self.serving() as server:
                self.send(server, {"operation": "push", "manifest": fa.manifest()})
                result = self.send(server, {"operation": "request_review", "body": fa.ANSWERS})
        self.assertEqual(result["result"].get("answers"), "posted")
        self.assertEqual([p.rsplit("/", 1)[1] for p, _, _ in self.fake.posts()], ["comments"])
        kick.assert_called_once()

    def test_a_pat_reviewer_is_still_requested_on_github(self):
        self.loop["tokens"]["review"] = self.loop["apps"].pop("review")["key_file"]
        with mock.patch.object(review_kick, "kick") as kick:
            with self.serving() as server:
                self.send(server, {"operation": "push", "manifest": fa.manifest()})
                self.assertTrue(self.send(server, {"operation": "request_review"})["ok"])
        self.assertEqual([p.rsplit("/", 1)[1] for p, _, _ in self.fake.posts()],
                         ["requested_reviewers"])
        kick.assert_not_called()

    def test_an_issue_fixs_pr_is_not_requested_for_an_app_reviewer(self):
        with mock.patch.object(broker, "_write", side_effect=AssertionError("no request")):
            broker.request_issue_pr_review(self.loop, repo=fa.REPO, pr=9, login="fix")
        audit = (Path(self.loop["state_dir"]) / "broker-audit.jsonl").read_text()
        self.assertIn('"operation": "request_review"', audit)


class Explain(AppSeat):
    def report(self, **pr_extra):
        st = state_mod.state_for(self.loop)
        live = {**self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7"), **pr_extra}
        facts = {"pr": live, "reviews": [], "armed": True, "read_at": 0, "shadow_hold": ("", "")}
        return explain.explain(self.loop, st, 7, facts)

    def test_the_next_step_is_hermes_dk_review_not_a_github_request(self):
        report = self.report()
        if report["next"]["kind"] == "review-request":
            self.assertIn("hermes dk review --loop widgets --pr 7", report["next"]["action"])
            self.assertNotIn("requested_reviewers", report["next"]["action"])
        else:
            self.skipTest(f"explain chose {report['next']['kind']} for these facts")


class ExplainReviewOnly(AppSeat):
    """#1211: the review-only author's next step names `hermes dk review` for an App reviewer."""

    def test_the_authors_next_step_names_hermes_dk_review(self):
        self.loop["review_only"] = ["boss"]
        st = state_mod.state_for(self.loop)
        live = {**self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7"), "user": {"login": "boss"}}
        head = live["head"]["sha"]
        reviews = [{"id": 1, "state": "CHANGES_REQUESTED", "commit_id": head,
                    "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "review", "id": 2}}]
        facts = {"pr": live, "reviews": reviews, "armed": True, "read_at": 0,
                 "shadow_hold": ("", "")}
        report = explain.explain(self.loop, st, 7, facts)
        self.assertEqual(report["next"]["kind"], "author-push", report["next"])
        self.assertIn("hermes dk review --loop widgets --pr 7", report["next"]["action"])
        self.assertNotIn("re-requests review", report["next"]["action"])


class ExplainPendingHandoff(AppSeat):
    """#1213: an App reviewer is never in ``requested_reviewers``, so the host's handoff at the
    head is the pending request ``explain`` reasons from (blockers, state line, next step)."""

    def report(self, reviews=None):
        st = state_mod.state_for(self.loop)
        live = self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7")
        facts = {"pr": live, "reviews": self.fake.reviews if reviews is None else reviews,
                 "armed": True, "read_at": 0, "shadow_hold": ("", "")}
        return explain.explain(self.loop, st, 7, facts)

    def setUp(self):
        super().setUp()
        self.loop["seats"]["reviewer"]["route"] = "widgets-review"

    def hand_off(self, acknowledged=True):
        """The fixer's push and request through the real run broker and the real ``kick``; only
        the gateway's answer (``routes.fire``) is faked: 2xx, or a delivery that failed."""
        with mock.patch.object(routes, "fire", return_value=acknowledged) as fire:
            with self.serving() as server:
                self.assertTrue(self.send(server, {"operation": "push",
                                                   "manifest": fa.manifest()})["ok"])
                self.assertTrue(self.send(server, {"operation": "request_review"})["ok"])
        fire.assert_called_once()

    def test_a_failed_delivery_is_no_pending_request(self):
        """Vex F1 on #1943: the broker audits the request before the delivery, so an audit line
        alone must not read as a handoff the gate received."""
        self.hand_off(acknowledged=False)
        audit = (Path(self.loop["state_dir"]) / "broker-audit.jsonl").read_text()
        self.assertIn('"operation": "request_review"', audit)        # authorized and audited
        report = self.report()
        self.assertFalse(report["request_pending"], report)
        self.assertNotIn("review requested from", report["state_line"])
        self.assertTrue([b for b in report["blockers"] if "no review request exists" in b],
                        report["blockers"])
        self.assertNotIn("started none", report["next"]["action"])

    def test_a_retry_that_lands_after_a_failed_delivery_is_pending(self):
        self.hand_off(acknowledged=False)
        with mock.patch.object(routes, "fire", return_value=True):
            self.assertTrue(review_kick.kick(self.loop, self.fake.api(
                self.loop, f"/repos/{fa.REPO}/pulls/7"), fa.NEW_HEAD, "fix", "sweep-review-7"))
        self.assertTrue(self.report()["request_pending"])

    def test_a_crash_between_delivery_and_its_record_reads_as_no_request(self):
        """The record is written after the 2xx: losing it (a crash, a refused write) leaves
        explain saying no request exists, never the reverse; the delivery itself still counts."""
        with mock.patch.object(state_mod.LoopState, "_save_strict", side_effect=OSError("full")), \
                mock.patch.object(routes, "fire", return_value=True):
            self.assertTrue(review_kick.kick(self.loop, self.fake.api(
                self.loop, f"/repos/{fa.REPO}/pulls/7"), fa.NEW_HEAD, "fix", "kick-review-7"))
        self.fake.pr_head = fa.NEW_HEAD
        self.assertFalse(self.report()["request_pending"])

    def test_a_late_ack_for_an_older_head_keeps_the_current_heads(self):
        """Vex F3 on #1943: deliveries complete out of order; an older head's acknowledgement
        arriving after the current head's must not erase the current head's handoff."""
        self.fake.pr_head = fa.NEW_HEAD
        live = self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7")
        with mock.patch.object(routes, "fire", return_value=True):
            self.assertTrue(review_kick.kick(self.loop, live, fa.NEW_HEAD, "fix", "kick-review-7"))
            self.assertTrue(self.report()["request_pending"])
            self.assertTrue(review_kick.kick(self.loop, live, "e" * 40, "fix", "sweep-review-7"))
        report = self.report()
        self.assertTrue(report["request_pending"], report)
        self.assertFalse([b for b in report["blockers"] if "no review request exists" in b],
                         report["blockers"])
        self.assertTrue(review_kick.app_handoff_delivered(self.loop, 7, "e" * 40))

    def test_late_acks_for_many_older_heads_keep_the_live_head_and_the_bound(self):
        """#1946 (Vex F3/F4 on #1943): the live head's delivery is requested last but
        acknowledged first; more than ``HANDOFF_HEADS_KEPT`` late acknowledgements for older
        heads must not evict it, and the record still holds no more than the bound."""
        self.fake.pr_head = fa.NEW_HEAD
        live = self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7")
        older = [f"{i:040x}" for i in range(1, 21)]  # 20 > HANDOFF_HEADS_KEPT, fixed on purpose
        self.assertLess(review_kick.HANDOFF_HEADS_KEPT, len(older))
        with mock.patch.object(routes, "fire", return_value=True):
            with mock.patch.object(review_kick.time, "time", return_value=1000.0):
                self.assertTrue(review_kick.kick(self.loop, live, fa.NEW_HEAD, "fix", "kick-7"))
            for i, head in enumerate(older):          # requested earlier, acknowledged later
                with mock.patch.object(review_kick.time, "time", return_value=float(i + 1)):
                    self.assertTrue(review_kick.kick(self.loop, live, head, "fix", "sweep-7"))
        report = self.report()
        self.assertTrue(report["request_pending"], report)
        self.assertTrue(review_kick.app_handoff_delivered(self.loop, 7, fa.NEW_HEAD))
        record = state_mod.read_record(state_mod.state_for(self.loop), review_kick.HANDOFFS)
        kept = review_kick._acknowledged_heads(record["7"])
        self.assertEqual(len(kept), review_kick.HANDOFF_HEADS_KEPT)
        self.assertEqual(kept[-1], fa.NEW_HEAD)
        self.assertEqual(kept[:-1], older[-(review_kick.HANDOFF_HEADS_KEPT - 1):])
        self.assertFalse(review_kick.app_handoff_delivered(self.loop, 7, older[0]))

    def test_a_handoff_recorded_by_the_earlier_format_still_counts(self):
        """A record written before F3 (one head per PR) still reads, and the next delivery
        keeps it."""
        self.fake.pr_head = fa.NEW_HEAD
        (Path(self.loop["state_dir"])).mkdir(parents=True, exist_ok=True)
        (Path(self.loop["state_dir"]) / review_kick.HANDOFFS).write_text('{"7": "%s"}' % fa.NEW_HEAD)
        self.assertTrue(self.report()["request_pending"])
        live = self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7")
        with mock.patch.object(routes, "fire", return_value=True):
            self.assertTrue(review_kick.kick(self.loop, live, "e" * 40, "fix", "sweep-review-7"))
        self.assertTrue(self.report()["request_pending"])

    def test_a_handoff_list_from_the_previous_format_reads_and_keeps_its_order(self):
        """Vex F2 on #2654: a record written as a list of heads (the format before #1946)
        still counts for explain, and the next delivery keeps every listed head, in its order,
        ahead of the newly requested one."""
        self.fake.pr_head = fa.NEW_HEAD
        legacy = ["1" * 40, "2" * 40, fa.NEW_HEAD]
        (Path(self.loop["state_dir"])).mkdir(parents=True, exist_ok=True)
        (Path(self.loop["state_dir"]) / review_kick.HANDOFFS).write_text(
            '{"7": ["%s", "%s", "%s"]}' % tuple(legacy))
        self.assertTrue(self.report()["request_pending"])
        live = self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7")
        with mock.patch.object(routes, "fire", return_value=True):
            self.assertTrue(review_kick.kick(self.loop, live, "e" * 40, "fix", "sweep-review-7"))
        self.assertTrue(self.report()["request_pending"])
        record = state_mod.read_record(state_mod.state_for(self.loop), review_kick.HANDOFFS)
        self.assertEqual(review_kick._acknowledged_heads(record["7"]), legacy + ["e" * 40])

    def test_an_unreadable_record_is_no_request(self):
        self.hand_off()
        (Path(self.loop["state_dir"]) / review_kick.HANDOFFS).write_text('{"7": ')   # torn
        self.assertFalse(self.report()["request_pending"])

    def test_a_full_reviewer_seat_is_named_while_the_handoff_is_pending(self):
        """Vex F2 on #1943: the reviewer-capacity attribution, reachable in App mode only once
        the handoff counts as the pending request."""
        self.hand_off()
        st = state_mod.state_for(self.loop)
        self.assertTrue(st.acquire("reviewer", gate.seat_key(self.loop, 9), head="f" * 40,
                                   why="reviewer run"))
        report = self.report()
        self.assertTrue([b for b in report["blockers"]
                         if "no capacity: reviewer seat at capacity 1/1 on other PRs" in b],
                        report["blockers"])
        self.assertEqual(report["next"]["kind"], "retry", report["next"])
        self.assertIn("the reviewer gate will queue it at capacity 1/1", report["next"]["action"])

    def test_a_delivered_handoff_reads_as_a_pending_request(self):
        self.hand_off()
        report = self.report()
        self.assertTrue(report["request_pending"], report)
        self.assertIn("review requested from review", report["state_line"])
        self.assertFalse([b for b in report["blockers"] if "no review request exists" in b],
                         report["blockers"])
        self.assertTrue([b for b in report["blockers"]
                         if "review request for review is pending" in b], report["blockers"])
        self.assertEqual(report["next"]["kind"], "retry", report["next"])
        self.assertIn("the pending request for review started none", report["next"]["action"])

    def test_the_sweeps_delivery_counts_too(self):
        """The sweep delivers through the same ``kick``; its acknowledged delivery is recorded."""
        self.fake.pr_head = fa.NEW_HEAD
        self.assertFalse(self.report()["request_pending"])
        with mock.patch.object(routes, "fire", return_value=True):
            review_kick.kick(self.loop, self.fake.api(self.loop, f"/repos/{fa.REPO}/pulls/7"),
                             fa.NEW_HEAD, "fix", "sweep-review-7")
        self.assertTrue(self.report()["request_pending"])

    def test_no_handoff_at_this_head_is_no_request(self):
        self.fake.pr_head = fa.NEW_HEAD
        report = self.report()
        self.assertFalse(report["request_pending"])
        self.assertTrue([b for b in report["blockers"] if "no review request exists" in b],
                        report["blockers"])

    def test_a_verdict_at_the_head_ends_it(self):
        self.hand_off()
        verdict = {"id": 42, "state": "APPROVED", "commit_id": fa.NEW_HEAD,
                   "submitted_at": "2026-01-02T00:00:00Z", "user": {"login": "review", "id": 2}}
        self.assertFalse(self.report(self.fake.reviews + [verdict])["request_pending"])

    def test_a_pat_reviewer_is_still_read_from_github(self):
        self.loop["tokens"]["review"] = self.loop["apps"].pop("review")["key_file"]
        self.fake.pr_head = fa.NEW_HEAD
        (Path(self.loop["state_dir"])).mkdir(parents=True, exist_ok=True)
        (Path(self.loop["state_dir"]) / review_kick.HANDOFFS).write_text('{"7": "%s"}' % fa.NEW_HEAD)
        self.assertFalse(self.report()["request_pending"])
        self.fake.requested = ["review"]
        self.assertTrue(self.report()["request_pending"])


class WatchdogBackstop(__import__("test_fixer_gating").Base):
    """The sweep delivers a quiet head's review to the gate in App mode: an App reviewer never
    shows as a pending requested reviewer, so #532's condition alone would never fire."""

    def sweep(self, listing=None, pr_reads=None):
        """Two sweeps (the first only snapshots). ``pr_reads`` maps a PR number to what a fresh
        ``gh.pr`` read of it answers now; any other number reads as the open #7."""
        import test_fixer_gating as fg
        from diaktoros import gh, pr_hold
        from scripts import watchdog
        open_prs = {"side_effect": listing} if listing else {"return_value": [fg.LIVE]}
        reads = pr_reads or {}

        def read_pr(loop, number):
            return reads[number] if number in reads else fg.LIVE

        with mock.patch.object(watch_mode, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", **open_prs), \
             mock.patch.object(gh, "pr", side_effect=read_pr), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(pr_hold, "read", return_value=(None, "")), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(review_kick, "kick", return_value=True) as kick:
            watchdog.sweep_loop(self.loop, self.st)          # first sweep: snapshot only
            watchdog.sweep_loop(self.loop, self.st)
        return kick

    def test_an_app_reviewers_quiet_head_is_delivered_to_the_gate(self):
        self.loop = {**self.loop, "apps": {self.loop["reviewer_seat"]: {"app_id": 1,
                                                                         "key_file": "/k"}}}
        self.loop["tokens"] = {k: v for k, v in self.loop["tokens"].items()
                               if k.lower() != self.loop["reviewer_seat"].lower()}
        self.assertTrue(config.reviewer_is_app(self.loop))
        self.assertTrue(self.sweep().called)

    def test_a_pat_reviewer_not_requested_is_left_alone(self):
        self.assertFalse(config.reviewer_is_app(self.loop))
        self.assertFalse(self.sweep().called)

    def test_the_sweep_drops_a_closed_prs_handoffs_and_keeps_an_open_ones(self):
        """#1946: the sweep prunes ``review-handoffs.json`` with the open PR numbers it read
        (#7 is open, #8 merged or closed, which a fresh read of #8 confirms), as it does the
        conflict record."""
        path = self.st.dir / review_kick.HANDOFFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"7": {"%s": 5.0}, "8": {"%s": 6.0}}' % ("a" * 40, "b" * 40))
        self.sweep(pr_reads={8: {"number": 8, "state": "closed"}})
        record = state_mod.read_record(self.st, review_kick.HANDOFFS)
        self.assertEqual(record, {"7": {"a" * 40: 5.0}})

    def test_an_in_flight_handoff_requested_before_the_listing_survives_a_reopen(self):
        """Vex F1 (round 2) on #2654: #8's delivery is requested at t=99, the listing (t=100)
        does not name #8 because it is closed, then #8 reopens and the delivery is acknowledged
        before the prune. A fresh read says #8 is open, so its entry stays; the closed #9's
        (earlier list format) still goes."""
        import test_fixer_gating as fg
        path = self.st.dir / review_kick.HANDOFFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"9": ["%s"]}' % ("c" * 40))
        calls = []

        def listing(loop, errors=None, **_k):
            calls.append(1)
            if len(calls) == 2:                               # the second (pruning) sweep
                review_kick._record_handoff(self.loop, 8, "d" * 40, 99.0)   # before the listing
            return [fg.LIVE]

        self.sweep(listing, pr_reads={8: {"number": 8, "state": "open"},
                                      9: {"number": 9, "state": "closed"}})
        self.assertEqual(len(calls), 2)
        record = state_mod.read_record(self.st, review_kick.HANDOFFS)
        self.assertEqual(record, {"8": {"d" * 40: 99.0}})

    def test_a_pr_reopened_after_the_listing_without_a_new_handoff_keeps_its_entry(self):
        """Vex F1 (round 2) on #2654: #8 was handed off long ago (an old time, and an entry in
        the earlier list format for #10), is missing from the listing, and reopened before the
        prune without any new delivery. The fresh reads say open, so both entries stay."""
        path = self.st.dir / review_kick.HANDOFFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"8": {"%s": 1.0}, "10": ["%s"]}' % ("d" * 40, "e" * 40))
        self.sweep(pr_reads={8: {"number": 8, "state": "open"},
                             10: {"number": 10, "state": "open"}})
        record = state_mod.read_record(self.st, review_kick.HANDOFFS)
        self.assertEqual(record, {"8": {"d" * 40: 1.0}, "10": ["e" * 40]})

    def test_an_unknown_read_or_an_entry_changed_since_the_candidates_keeps_it(self):
        """A failed fresh read (None) is not a confirmation, and an entry rewritten between the
        candidates' read and the locked rewrite (a handoff landing) is left for a later sweep."""
        from diaktoros import gh
        st = self.st
        path = st.dir / review_kick.HANDOFFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"8": {"%s": 1.0}, "9": {"%s": 2.0}}' % ("d" * 40, "c" * 40))

        def read_pr(loop, number):
            if number == 9:                   # #9 reads closed, but a handoff lands meanwhile
                review_kick._record_handoff(self.loop, 9, "f" * 40, 3.0)
                return {"number": 9, "state": "closed"}
            return None                       # #8: the read failed
        with mock.patch.object(gh, "pr", side_effect=read_pr):
            self.assertEqual(review_kick.prune(self.loop, st, [7]), [])
        record = state_mod.read_record(st, review_kick.HANDOFFS)
        self.assertEqual(set(record), {"8", "9"})
        self.assertEqual(review_kick._acknowledged_heads(record["9"]), ["c" * 40, "f" * 40])


if __name__ == "__main__":
    unittest.main()
