"""#504: a conflicted review-only PR's facts are assessed once per (head, base tip), and doctor says
when ``review_only_update`` can never push (the base does not require up-to-date branches).

The sweep tests drive ``sweep_loop`` (the enforcing entry point); the dry merge itself is the
counted seam. Doctor is driven through ``check_review_only_update`` and ``check_loop``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_conflict_speed as cs  # noqa: E402
import test_doctor_environment  # noqa: E402
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, doctor, gate, gh, review_only_conflict, safe_push  # noqa: E402
from diaktoros import watch_conflicts  # noqa: E402

OWNER_PR = {**fg.LIVE, "user": {"login": "owner"},
            "head": {"sha": fg.HEAD, "ref": "feat", "repo": {"full_name": fg.REPO}}}
DIRTY = {**OWNER_PR, "mergeable": False, "mergeable_state": "dirty"}
BEHIND = {**OWNER_PR, "mergeable": True, "mergeable_state": "behind"}
CLEAN = {**OWNER_PR, "mergeable": True, "mergeable_state": "clean"}
NEW_HEAD = "d" * 40
NEW_TIP = "e" * 40


def dry(conflicted=("a.py",), clean=False):
    return {"commits": [("1" * 40, "Change a thing (#428)")], "conflicted": list(conflicted),
            "workflows": False, "clean": clean}


class Cache(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True, update=False)
        self.notify = mock.Mock()
        self.tip = cs.TIP

    def set_push(self, enabled, update=None):
        super().set_push(enabled)
        raw = {**fg.raw_loop(enabled), "state_dir": str(self.root / "state"),
               "review_only": ["owner"], "review_only_update": bool(update)}
        (config.config_dir() / "one.json").write_text(json.dumps(raw))
        self.loop = config.load_id("one")
        self.st = fg.state_mod.state_for(self.loop)

    def sweep(self, live):
        """One sweep with the PR read as ``live``; its notices go to ``self.notify``."""
        from scripts import watchdog
        patches = cs.Detection.patches(self, [], prs=[live])[2]
        patches = [p for p in patches if getattr(p, "attribute", "") not in ("pr", "base_tip")]
        patches += [mock.patch.object(gh, "pr", side_effect=lambda *a, **k: live),
                    mock.patch.object(watch_conflicts, "base_tip",
                                      side_effect=lambda loop: self.tip),
                    mock.patch.object(watchdog.observer, "notify", self.notify),
                    mock.patch.object(gate, "enqueue_isolated", return_value="queued")]
        for patch in patches:
            patch.start()
        try:
            watchdog.sweep_loop(self.loop, self.st)
        finally:
            for patch in reversed(patches):
                patch.stop()

    def test_two_sweeps_at_the_same_head_and_base_run_the_dry_merge_once(self):
        self.sweep(CLEAN)                               # the first sweep only snapshots heads
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge:
            for _ in range(3):
                self.sweep(DIRTY)
        self.assertEqual(merge.call_count, 1)

    def test_a_new_head_or_a_new_base_runs_it_again(self):
        self.sweep(CLEAN)
        other = {**DIRTY, "head": {**DIRTY["head"], "sha": NEW_HEAD}}
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge:
            self.sweep(DIRTY)
            self.sweep(DIRTY)
            self.assertEqual(merge.call_count, 1)
            self.sweep(other)                           # a new head
            self.sweep(other)
            self.assertEqual(merge.call_count, 2)
            self.tip = NEW_TIP                          # a new base
            self.sweep(other)
            self.sweep(other)
            self.assertEqual(merge.call_count, 3)

    def test_the_notice_is_the_same_from_the_cache(self):
        self.sweep(CLEAN)
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge:
            self.sweep(DIRTY)
            self.sweep(DIRTY)
        self.assertEqual(merge.call_count, 1)
        texts = [c.kwargs["next_turn"] for c in self.notify.call_args_list]
        self.assertEqual(len(texts), 2, texts)
        self.assertEqual(texts[0], texts[1])
        self.assertIn("the conflict is in one file: a.py", texts[0])
        self.assertIn("#428", texts[0])

    def test_a_behind_pr_whose_merge_is_not_clean_attempts_the_push_once(self):
        self.set_push(True, update=True)
        self.sweep(CLEAN)
        with mock.patch.object(safe_push, "dry_merge",
                               return_value=dry(conflicted=(), clean=True)) as merge, \
             mock.patch.object(safe_push, "update_branch",
                               side_effect=safe_push.NotClean("not clean")) as pushed:
            for _ in range(3):
                self.sweep(BEHIND)
        self.assertEqual((merge.call_count, pushed.call_count), (1, 1))
        self.assertEqual(self.notify.call_count, 0)     # behind, not conflicting: no notice

    def test_a_stop_that_is_not_about_the_pair_is_tried_again(self):
        # Shadow on #1151: pushes off, then on, at the same head and base tip.
        self.set_push(False, update=True)
        self.sweep(CLEAN)
        with mock.patch.object(safe_push, "dry_merge",
                               return_value=dry(conflicted=(), clean=True)) as merge, \
             mock.patch.object(safe_push, "update_branch",
                               return_value={"new_head": NEW_HEAD}) as pushed:
            self.sweep(BEHIND)
            self.assertEqual(pushed.call_count, 0)          # pushes are off: stopped
            self.set_push(True, update=True)
            self.sweep(BEHIND)
        self.assertEqual((merge.call_count, pushed.call_count), (1, 1))   # dry merge kept

    def test_a_refused_or_failed_push_is_tried_again_on_the_next_sweep(self):
        self.set_push(True, update=True)
        self.sweep(CLEAN)
        for failure in (safe_push.broker.BrokerDenied("cannot verify live PR"),
                        safe_push.PushFailure("push not verified")):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(safe_push, "dry_merge",
                                      return_value=dry(conflicted=(), clean=True)), \
                    mock.patch.object(safe_push, "update_branch",
                                      side_effect=[failure, {"new_head": NEW_HEAD}]) as pushed:
                self.st.review_only_conflicts.unlink(missing_ok=True)
                self.sweep(BEHIND)
                self.sweep(BEHIND)
                self.assertEqual(pushed.call_count, 2)

    def test_an_unreadable_base_tip_caches_nothing(self):
        self.sweep(CLEAN)
        self.tip = None
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge:
            self.sweep(DIRTY)
            self.sweep(DIRTY)
        self.assertEqual(merge.call_count, 2)

    def test_a_fork_is_assessed_every_sweep_and_never_cached(self):
        """#1548: the docs say a fork stop is not cached; it never runs a dry merge or pushes."""
        self.set_push(True, update=True)
        self.sweep(CLEAN)
        fork = {**DIRTY, "head": {**DIRTY["head"], "repo": {"full_name": "someone/fork"}}}
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge, \
                mock.patch.object(review_only_conflict, "assess",
                                  wraps=review_only_conflict.assess) as assess, \
                mock.patch.object(review_only_conflict, "update",
                                  wraps=review_only_conflict.update) as upd:
            self.sweep(fork)
            self.sweep(fork)
        self.assertEqual(assess.call_count, 2)
        self.assertEqual(upd.call_count, 2)
        merge.assert_not_called()
        self.assertFalse(self.st.review_only_conflicts.exists())

    def test_the_docs_qualify_what_the_conflict_cache_does_not_hold(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
        text = docs.read_text()
        self.assertIn("**What is not cached:** a fork branch", text)
        self.assertIn("unreadable", text.split("**What is not cached:**")[1][:600])

    def test_the_docs_say_a_retry_fetches_again_in_the_update(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
        text = docs.read_text()
        self.assertNotIn("only the fetch is held back", text)
        self.assertNotIn("does not fetch again", text)
        self.assertIn("fetches the branch and the base again", text)

    def test_the_docs_qualify_which_retries_reach_the_fetch(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
        text = docs.read_text()
        self.assertIn("A retry that is an authorized update attempt", text)
        self.assertIn("A retry that stops before that point fetches nothing", text)
        self.assertIn("fetches the branch and the base again", text)

    def test_turning_the_setting_on_assesses_again(self):
        self.sweep(CLEAN)
        with mock.patch.object(safe_push, "dry_merge", return_value=dry()) as merge:
            self.sweep(DIRTY)
            self.loop["review_only_update"] = True
            self.sweep(DIRTY)
        self.assertEqual(merge.call_count, 2)


class Doctor(unittest.TestCase):
    """``check_review_only_update`` with the setting on and the token able to push."""

    def setUp(self):
        self.loop = {"id": "w", "repo": "acme/w", "base": "main", "review_only_update": True}
        self.rules, self.protection = [], None

    def fake(self, loop, path, method="GET", body=None, login=None):
        if "/rules/branches/" in path:
            if self.rules is None:
                return gh.Response(None, "HTTP 403", 403, {})
            return gh.Response(self.rules, "", 200, {})
        if "required_status_checks" in path:
            if self.protection == "forbidden":
                return gh.Response(None, "HTTP 403", 403, {})
            if self.protection is None:
                return gh.Response(None, "HTTP 404", 404, {})
            return gh.Response(self.protection, "", 200, {})
        return gh.Response(None, "HTTP 404", 404, {})

    def check(self):
        repo = {"permissions": {"push": True}}
        with mock.patch.object(gh, "request", side_effect=self.fake), \
             mock.patch.object(gh, "api", return_value=repo), \
             mock.patch.object(doctor.config, "seat_login", return_value="fix"), \
             mock.patch.object(doctor.config, "unattended_fixer_push_enabled", return_value=True):
            return doctor.check_review_only_update(self.loop)

    def test_a_base_without_up_to_date_branches_is_noted(self):
        got = self.check()
        self.assertEqual(got.status, doctor.UNKNOWN)
        self.assertIn("does not require branches to be up to date", got.detail)
        self.assertIn("only the conflict notice applies", got.detail)
        self.protection = {"strict": False, "contexts": ["ci"]}
        self.assertEqual(self.check().status, doctor.UNKNOWN)

    def test_a_ruleset_or_classic_protection_that_requires_it_is_verified(self):
        self.rules = [{"type": "required_status_checks",
                       "parameters": {"strict_required_status_checks_policy": True,
                                      "required_status_checks": []}}]
        self.assertEqual(self.check().status, doctor.VERIFIED)
        self.rules, self.protection = [], {"strict": True, "contexts": []}
        self.assertEqual(self.check().status, doctor.VERIFIED)

    def test_an_unreadable_rule_is_reported_as_unknown_not_as_off(self):
        self.rules, self.protection = None, "forbidden"
        got = self.check()
        self.assertEqual(got.status, doctor.UNKNOWN)
        self.assertIn("could not read", got.detail)
        self.assertNotIn("does not require", got.detail)


class Preflight(test_doctor_environment.Environment):
    """The note reaches ``check_loop``'s report (what ``hermes dk doctor`` prints)."""

    def test_the_loop_preflight_notes_a_base_that_cannot_be_pushed_to(self):
        self.loop["review_only_update"] = True
        self.loop["unattended_fixer_push"] = True
        self.rules, self.protection = [], {"contexts": ["ci"], "strict": False}
        with mock.patch.object(gh, "api", return_value={"permissions": {"push": True}}):
            got = self.run_check()["review-only-update"]
        self.assertEqual(got.status, doctor.UNKNOWN)
        self.assertIn("does not require branches to be up to date", got.detail)
        self.protection = {"contexts": ["ci"], "strict": True}
        with mock.patch.object(gh, "api", return_value={"permissions": {"push": True}}):
            self.assertEqual(self.run_check()["review-only-update"].status, doctor.VERIFIED)


if __name__ == "__main__":
    unittest.main()
