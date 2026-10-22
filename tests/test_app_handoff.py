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
from diaktoros import broker, config, gate, github_app, review_kick, state as state_mod  # noqa: E402


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


class AppSeat(fa.Base):
    """The real run broker, with the reviewer seat a GitHub App (its key mapped in ``apps``); its
    principal and token are the App path's (``github_app``), proven live in M1b."""

    def setUp(self):
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
        return gate.explain(self.loop, st, 7, facts)

    def test_the_next_step_is_hermes_dk_review_not_a_github_request(self):
        report = self.report()
        if report["next"]["kind"] == "review-request":
            self.assertIn("hermes dk review --loop widgets --pr 7", report["next"]["action"])
            self.assertNotIn("requested_reviewers", report["next"]["action"])
        else:
            self.skipTest(f"explain chose {report['next']['kind']} for these facts")


class WatchdogBackstop(__import__("test_fixer_gating").Base):
    """The sweep delivers a quiet head's review to the gate in App mode: an App reviewer never
    shows as a pending requested reviewer, so #532's condition alone would never fire."""

    def sweep(self):
        import test_fixer_gating as fg
        from diaktoros import gh, pr_hold
        from scripts import watchdog
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(gh, "pr", return_value=fg.LIVE), \
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


if __name__ == "__main__":
    unittest.main()
