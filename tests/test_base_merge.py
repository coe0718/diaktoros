"""#1785: the host merges the base into a loop PR that fell behind, and a clean merge keeps its
approval (``update_behind``).

Real Git, a local bare remote. The PR branch ``feat`` changes line 1 of ``a.py``; ``main`` then
moves: on ``c.py`` only (the merge touches nothing the PR changed), on line 10 of ``a.py`` (a
clean merge that touches the PR's file), or on line 1 of ``a.py`` (a conflict). Nothing here
reaches the network.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import base_merge, broker, ci, config, gate, gh, observer, ready  # noqa: E402
from diaktoros import safe_push, watch_conflicts  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.doctor import identity_checks  # noqa: E402

REPO = "acme/widgets"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
       "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}
LINES = "".join(f"line {n}\n" for n in range(1, 11))
GREEN = ci.CIState(passed=["tests"])


def git(*args):
    return subprocess.check_output(["git", *args], env=ENV, text=True).strip()


def approval(head, review_id=11, state="APPROVED", login="rev", number=7, at="2026-11-10T01:00:00Z"):
    return {"id": review_id, "user": {"login": login, "id": 9}, "state": state, "commit_id": head,
            "submitted_at": at, "pull_request_url": f"https://api.github.com/repos/{REPO}/pulls/{number}"}


class Base(unittest.TestCase):
    MAIN = "disjoint"           # what main changes after the PR branched: disjoint, same-file, conflict

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.src = src = self.root / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", str(src))
        (src / "a.py").write_text(LINES)
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "base")
        git("-C", str(src), "checkout", "-qb", "feat")
        (src / "a.py").write_text(LINES.replace("line 1\n", "line one\n"))
        git("-C", str(src), "commit", "-qam", "the PR")
        self.head = git("-C", str(src), "rev-parse", "HEAD")
        git("-C", str(src), "checkout", "-q", "main")
        if self.MAIN == "disjoint":
            (src / "c.py").write_text("c\n")
        else:
            old, new = ("line 10\n", "line ten\n") if self.MAIN == "same-file" else ("line 1\n", "line 1!\n")
            (src / "a.py").write_text(LINES.replace(old, new))
        git("-C", str(src), "add", "-A")
        git("-C", str(src), "commit", "-qm", "Another (#430)")
        self.tip = git("-C", str(src), "rev-parse", "HEAD")
        self.remote = str(self.root / "remote.git")
        git("init", "-q", "--bare", self.remote)
        git("-C", str(src), "push", "-q", self.remote, "main", "feat")
        token = self.root / "token"
        token.write_text("not-a-real-token")
        self.loop = {"id": "w", "repo": REPO, "base": "main", "read_token": "read", "cap": 3,
                     "fixers": ["fix"], "reviewers": ["rev"], "review_only": ["owner"],
                     "tokens": {"fix": str(token), "read": str(token)},
                     "seats": {"fixer": {"login": "fix"}, "reviewer": {"login": "rev"}},
                     "update_behind": True, "unattended_fixer_push": True, "attribution": False,
                     "state_dir": str(self.root / "state")}
        (self.root / "state").mkdir()
        self.st = state_mod.LoopState(self.loop)
        env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/does/not/exist"})
        env.start()
        self.addCleanup(env.stop)
        self.live = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                     "mergeable": True, "mergeable_state": "behind",
                     "base": {"ref": "main", "repo": {"full_name": REPO}},
                     "head": {"sha": self.head, "ref": "feat", "repo": {"full_name": REPO}}}
        self.reviews = [approval(self.head)]
        self.left = []                       # ``ready``'s reasons at the old head besides behind
        self.notices = []

    def api(self, loop, path, method="GET", body=None, login=None):
        if path == "/user":
            return {"login": login, "id": 5}
        if path.endswith("/pulls/7"):
            return self.live
        if "/git/ref/heads/" in path:
            return {"ref": "refs/heads/feat",
                    "object": {"sha": git("--git-dir", self.remote, "rev-parse", "feat")}}
        return {}

    def isolated(self, loop, login, ident, remote):
        return safe_push.__dict__["_isolated_orig"](loop, login, ident, self.remote)

    def update(self):
        kinds = [(ready.GITHUB, base_merge.BEHIND)] + [(ready.FAILURE, why) for why in self.left]
        with mock.patch.object(gh, "api", side_effect=self.api), \
                mock.patch.object(safe_push, "_isolated", side_effect=self.isolated), \
                mock.patch.object(gh, "reviews", side_effect=lambda *a, **kw: self.reviews and list(self.reviews)), \
                mock.patch.object(watch_conflicts, "base_tip", return_value=self.tip), \
                mock.patch.object(ci, "read", return_value=GREEN), \
                mock.patch.object(ready, "assess_kinds", return_value=kinds), \
                mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **kw: self.notices.append((a, kw))):
            return base_merge.update(self.loop, self.st, 7, self.head, self.live)

    def remote_head(self):
        return git("--git-dir", self.remote, "rev-parse", "feat")

    def carried(self, head):
        return base_merge._records(self.loop).get(head)


# `_isolated` takes the remote through a seam; keep the real one reachable for the wrapper.
safe_push.__dict__.setdefault("_isolated_orig", safe_push._isolated)


class Carries(Base):
    def test_a_clean_merge_of_a_ready_approved_head_carries_its_approval(self):
        new = self.update()
        self.assertEqual(new, self.remote_head())
        parents = git("--git-dir", self.remote, "rev-list", "--parents", "-n1", new).split()
        self.assertEqual(parents[1:], [self.head, self.tip])
        self.assertEqual({k: self.carried(new)[k] for k in ("number", "from", "review", "base")},
                         {"number": 7, "from": self.head, "review": 11, "base": self.tip})
        (args, kw), = self.notices
        self.assertEqual((args[2], args[4]), ("updated", new))
        self.assertIn(f"the approval at {self.head[:7]} carries over", kw["outcome"])
        # Every reader sees it: the new head's verdict is the carried approval, so it is reviewed.
        latest = gate.latest_effective_review_at_head(self.reviews, self.loop, new)
        self.assertEqual((latest["id"], latest["commit_id"], latest["carried_from"]), (11, new, self.head))
        self.assertTrue(gate.reviewed_at_head(self.reviews, self.loop, new))
        self.assertIn(f"carried from {self.head[:7]}", base_merge.said(self.loop, latest))
        self.assertEqual(gate.verdicts(self.reviews, self.loop), [])     # no round spent

    def test_ready_counts_the_carried_approval_at_the_new_head_and_still_needs_ci(self):
        new = self.update()
        pr = {**self.live, "head": {**self.live["head"], "sha": new}, "mergeable_state": "clean"}
        with mock.patch.object(gh, "api", return_value={}):
            green = ready.assess(self.loop, self.st, pr, self.reviews, GREEN, pr)
            red = ready.assess(self.loop, self.st, pr, self.reviews, ci.CIState(failed=["tests"]), pr)
        self.assertFalse([w for w in green if "approved" in w or "verdict" in w], green)
        self.assertTrue([w for w in red if "required checks not green" in w], red)

    def test_a_chain_of_carries_follows_back_and_a_cycle_ends(self):
        new = self.update()
        newer = "e" * 40
        base_merge._record(self.st, newer, {"number": 7, "from": new, "review": 11, "at": 1})
        latest = gate.latest_effective_review_at_head(self.reviews, self.loop, newer)
        self.assertEqual((latest["commit_id"], latest["carried_from"]), (newer, new))
        a, b = "a" * 40, "b" * 40
        base_merge._record(self.st, a, {"number": 7, "from": b, "review": 11, "at": 2})
        base_merge._record(self.st, b, {"number": 7, "from": a, "review": 11, "at": 3})
        self.assertIsNone(gate.latest_effective_review_at_head(self.reviews, self.loop, a))


class ReadLive(Base):
    def setUp(self):
        super().setUp()
        self.new = self.update()

    def latest(self, reviews):
        return gate.latest_effective_review_at_head(reviews, self.loop, self.new)

    def test_nothing_carries_once_the_old_approval_is_dismissed_or_superseded(self):
        self.assertIsNotNone(self.latest(self.reviews))
        self.assertIsNone(self.latest([approval(self.head, state="DISMISSED")]))
        later = approval(self.head, review_id=12, state="CHANGES_REQUESTED", at="2026-11-10T02:00:00Z")
        self.assertIsNone(self.latest(self.reviews + [later]))
        self.assertFalse(gate.reviewed_at_head(self.reviews + [later], self.loop, self.new))

    def test_only_the_recorded_reviewers_approval_on_this_pr_carries(self):
        self.assertIsNone(self.latest([approval(self.head, review_id=13)]))        # another review
        self.assertIsNone(self.latest([approval(self.head, login="someone")]))     # not a reviewer
        self.assertIsNone(self.latest([approval(self.head, number=8)]))            # another PR

    def test_a_verdict_at_the_new_head_itself_wins_when_newer(self):
        mine = approval(self.new, review_id=14, state="CHANGES_REQUESTED", at="2026-11-10T03:00:00Z")
        self.assertEqual(self.latest(self.reviews + [mine])["id"], 14)


class NoCarry(Base):
    MAIN = "same-file"

    def test_the_base_touching_a_path_the_pr_changed_is_merged_and_reviewed_as_usual(self):
        new = self.update()
        self.assertEqual(new, self.remote_head())
        self.assertIsNone(self.carried(new))
        outcome = self.notices[0][1]["outcome"]
        self.assertIn("review resumes (no approval carried: a.py changed on both sides)", outcome)
        self.assertIsNone(gate.latest_effective_review_at_head(self.reviews, self.loop, new))
        self.assertFalse(gate.reviewed_at_head(self.reviews, self.loop, new))


class NotReady(Base):
    def test_an_unapproved_head_is_merged_but_nothing_carries(self):
        self.reviews = []
        new = self.update()
        self.assertTrue(new)
        self.assertIsNone(self.carried(new))
        self.assertIn("was not approved by the reviewer", self.notices[0][1]["outcome"])

    def test_a_head_held_by_anything_but_behind_carries_nothing(self):
        self.left = ["required checks not green — failed: tests"]
        new = self.update()
        self.assertIsNone(self.carried(new))
        self.assertIn("not ready to merge but for being behind: required checks not green",
                      self.notices[0][1]["outcome"])


class Conflict(Base):
    MAIN = "conflict"

    def test_a_conflict_is_never_pushed_nor_tried_twice_at_the_same_pair(self):
        self.assertEqual(self.update(), "")
        self.assertEqual(self.remote_head(), self.head)
        with mock.patch.object(safe_push, "update_branch") as push:
            self.assertEqual(self.update(), "")
        push.assert_not_called()
        self.assertEqual(self.notices, [])


class Waits(Base):
    def assert_waits(self):
        self.assertEqual(self.update(), "")
        self.assertEqual(self.remote_head(), self.head)

    def test_a_turn_in_flight_waits(self):
        db = self.root / "ledger.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE runs (repo TEXT, pr INTEGER, head TEXT, seat TEXT, state TEXT)")
        con.execute("INSERT INTO runs VALUES (?, 7, ?, 'reviewer', 'running')", (REPO, self.head))
        con.commit()
        con.close()
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            self.assertEqual(base_merge._in_flight(self.loop, 7), "a reviewer turn is running")
            self.assertEqual(base_merge._in_flight(self.loop, 8), "")
            self.assert_waits()

    def test_a_change_request_at_the_head_or_unread_reviews_wait(self):
        # Whether the fixer owes it or a ruling does at the cap: the head must not move under it.
        self.reviews = [approval(self.head, state="CHANGES_REQUESTED")]
        self.st.breach_set(7, {"head": self.head, "status": "awaiting-adjudication"})
        self.assert_waits()
        self.reviews = None                              # unreadable is not idle
        self.assert_waits()

    def test_a_person_hold_or_pushes_off_waits(self):
        self.st.human_hold_set(7, self.head, "touches human_paths")
        self.assert_waits()
        self.setUp()
        self.loop["unattended_fixer_push"] = False
        self.assert_waits()


class Routing(Base):
    def test_only_a_loop_pr_that_is_behind_is_the_hosts_with_the_setting_on(self):
        behind, dirty = dict(self.live), {**self.live, "mergeable_state": "dirty"}
        self.assertEqual(watch_conflicts.mergeability(self.loop, behind, self.head, "fix"), "conflict")
        self.assertEqual(watch_conflicts.mergeability({**self.loop, "update_behind": False},
                                                      behind, self.head, "fix"), "")
        self.assertEqual(watch_conflicts.mergeability(self.loop, behind, self.head, "owner"), "")
        conflicts = [(7, self.head, "", "fix"), (8, "f" * 40, "", "fix"), (9, "9" * 40, "", "owner")]
        live = {7: behind, 8: dirty, 9: behind}
        with mock.patch.object(base_merge, "update") as update:
            rest = base_merge.act(self.loop, self.st, conflicts, live)
        self.assertEqual([c[0] for c in rest], [8, 9])
        self.assertEqual(update.call_args[0][2:4], (7, self.head))

    def test_the_branch_update_refuses_off_and_a_review_only_author(self):
        with self.assertRaisesRegex(broker.BrokerDenied, "behind updates disabled"):
            safe_push.update_branch({**self.loop, "update_behind": False}, repo=REPO, number=7,
                                    head=self.head, branch="feat", behind=True)
        self.live["user"] = {"login": "owner"}
        with mock.patch.object(gh, "api", side_effect=self.api), \
                mock.patch.object(safe_push, "_isolated", side_effect=self.isolated), \
                self.assertRaisesRegex(broker.BrokerDenied, "not a loop author"):
            safe_push.update_branch(self.loop, repo=REPO, number=7, head=self.head, branch="feat",
                                    behind=True)
        self.assertEqual(self.update(), "")             # the sweep logs it and pushes nothing
        self.assertEqual(self.remote_head(), self.head)


class Identity(Base):
    def test_a_merged_tree_that_is_not_base_plus_the_pr_diff_is_refused(self):
        with safe_push._isolated(self.loop, "read", {"name": "x", "email": "x@x"}, self.remote) as (g, url):
            tip = safe_push._fetch_pair(g, url, "feat", self.head, "main")
            merged = safe_push._dry(g, self.head, tip)
            self.assertEqual(safe_push.tree_identity(g, self.head, tip, merged), "")
            base_tree = git("--git-dir", self.remote, "rev-parse", "main^{tree}")
            self.assertEqual(safe_push.tree_identity(g, self.head, tip, {**merged, "tree": base_tree}),
                             "the merged tree is not the base plus the PR's diff")


class Setting(unittest.TestCase):
    RAW = {"repo": REPO, "fixers": ["fix"], "reviewers": ["rev"], "read_token": "reader",
           "tokens": {}, "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-r"},
                                   "fixer": {"profile": "f", "login": "fix", "route": "w-f"}}}

    def test_off_by_default_strict_bool_and_on_the_form(self):
        self.assertIs(config.normalize(dict(self.RAW))["update_behind"], False)
        with self.assertRaises(config.ConfigError):
            config.normalize({**self.RAW, "update_behind": "yes"})
        got = settings_form.apply_settings(dict(self.RAW), {"update_behind": True})
        self.assertIs(got["update_behind"], True)
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn("  update_behind:", text)


class Doctor(Base):
    def check(self, push=True, strict=(True, ""), offline=False, **loop):
        with mock.patch.object(gh, "api", return_value={"permissions": {"push": push}}), \
                mock.patch("diaktoros.doctor.base_requires_up_to_date", return_value=strict):
            return identity_checks.check_update_behind({**self.loop, **loop}, offline)

    def test_each_state(self):
        from diaktoros import doctor
        self.assertIsNone(self.check(update_behind=False))
        self.assertEqual(self.check().status, doctor.VERIFIED)
        self.assertEqual(self.check(unattended_fixer_push=False).status, doctor.MISMATCH)
        self.assertEqual(self.check(push=False).status, doctor.MISMATCH)
        self.assertEqual(self.check(strict=(False, "")).status, doctor.UNKNOWN)
        self.assertEqual(self.check(offline=True).status, doctor.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
