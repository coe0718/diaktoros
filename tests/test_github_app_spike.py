"""The GitHub App spike harness repeats the steps docs/github-app-support.md lists (#1170).

The harness talks to GitHub, so these tests drive ``run`` and ``main`` of ``tools/github_app_spike.py``
against a recording stand-in for ``Spike.gh`` / ``Spike.app`` / ``Spike.graphql``: they check which
requests the harness makes (a person's PR, a label, a reopen) and which facts it prints, not GitHub.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("github_app_spike", ROOT / "tools" / "github_app_spike.py")
spike = importlib.util.module_from_spec(_spec)
sys.modules["github_app_spike"] = spike
_spec.loader.exec_module(spike)

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class FakeSpike(spike.Spike):
    """Records every request; answers just enough like GitHub for ``run`` to finish."""

    def __init__(self, person_token="", check_branch=""):
        super().__init__("o/r", {"reader": 1, "reviewer": 2, "fixer": 3}, "/nowhere", check_branch,
                         person_token)
        self.requests: list = []
        self.events: list = []  # requests, sleeps and pushes, in the order they happened
        self.unknown_left = 0
        self.prs = 0
        self.tokens = {r: {"token": "x", "exp": 1e12, "facts": {"length": 1}} for r in spike.ROLES}

    def app(self, role, method, path, body=None):
        if path == "/app":
            return 200, {"slug": f"{role}-app", "events": []}
        return 200, []

    def graphql(self, query, variables):
        self.events.append(("graphql", variables["n"]))
        state = "BLOCKED"
        if self.unknown_left:  # GitHub is still recomputing after a push
            self.unknown_left -= 1
            state = "UNKNOWN"
        return {"repository": {"pullRequest": {"reviewDecision": "REVIEW_REQUIRED",
                                               "mergeStateStatus": state}}}

    def gh(self, role, method, path, body=None):
        self.requests.append((role, method, path, body))
        self.events.append((role, method, path))
        if role == "person" and method == "PUT":
            self.unknown_left = 2
        if path.startswith("/users/"):
            return 200, {"login": path[7:], "id": {"reviewer-app[bot]": 22}.get(path[7:], 9),
                         "type": "Bot"}
        if path.endswith("/reviews?per_page=100"):
            return 200, [{"state": "APPROVED", "commit_id": SHA, "user": {"id": 22}}]
        if method == "GET" and "/git/ref/" in path:
            return 200, {"object": {"sha": SHA}}
        if method == "GET" and "/contents/" in path:
            return 200, {"sha": "blob"}
        if method == "PUT" and "/contents/" in path:
            return 200, {"commit": {"sha": SHA}}
        if method == "GET" and "/commits/" in path:
            return 200, {"commit": {"author": {}, "verification": {"verified": True, "reason": "ok"}}}
        if method == "POST" and path.endswith("/pulls"):
            self.prs += 1
            return 201, {"number": 10 + self.prs, "user": {"login": f"{role}[bot]", "type": "Bot"}}
        if method == "POST" and path.endswith("/reviews"):
            return 200, {"state": "APPROVED", "commit_id": SHA, "user": {"login": "r", "type": "Bot"}}
        return 200, {}

    def mine(self, role):
        return [(m, p, b) for r, m, p, b in self.requests if r == role]


def run_quietly(fake):
    out = io.StringIO()
    # git is the only subprocess left in ``run``; it must not touch the network here.
    done = mock.Mock(returncode=0, stdout=SHA + "\n", stderr="")

    def fake_git(argv, **kwargs):
        if "clone" in argv:
            Path(argv[-1]).mkdir()
            (Path(argv[-1]) / "app.py").write_text("x = 1\n")
        if "push" in argv:
            fake.events.append(("git push",))
            fake.unknown_left = 2
        return done

    with mock.patch.object(spike.time, "sleep", side_effect=lambda n: fake.events.append(("sleep", n))), \
            mock.patch.object(spike.subprocess, "run", side_effect=fake_git), \
            contextlib.redirect_stdout(out):
        spike.run(fake)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def step(lines, name):
    got = [line for line in lines if line["step"] == name]
    assert got, f"no {name} step in {[line['step'] for line in lines]}"
    return got[0]


class ReviewerProbes(unittest.TestCase):
    def test_run_probes_labels_and_reopen_after_close(self):
        fake = FakeSpike()
        lines = run_quietly(fake)
        probes = step(lines, "reviewer_permissions")
        self.assertEqual(probes["add_labels"], 200)
        self.assertEqual(probes["close_pr"], 200)
        self.assertEqual(probes["reopen_pr"], 200)
        calls = fake.mine("reviewer")
        label = next(i for i, (m, p, _) in enumerate(calls) if m == "POST" and p.endswith("/labels"))
        close = next(i for i, (m, _, b) in enumerate(calls) if m == "PATCH" and b == {"state": "closed"})
        reopen = next(i for i, (m, _, b) in enumerate(calls) if m == "PATCH" and b == {"state": "open"})
        self.assertLess(label, close)
        self.assertLess(close, reopen)


class StaleApproval(unittest.TestCase):
    def test_run_creates_a_persons_pr_and_rechecks_after_its_push(self):
        fake = FakeSpike(person_token="pat")
        lines = run_quietly(fake)
        got = step(lines, "stale_approval")
        self.assertNotIn("skipped", got)
        person = fake.mine("person")
        self.assertTrue(any(m == "POST" and p.endswith("/pulls") for m, p, _ in person))
        pushes = [i for i, (m, p, _) in enumerate(person) if m == "PUT"]
        self.assertEqual(len(pushes), 2, "one commit before the approval, one after")
        self.assertEqual(got["approve_state"], "APPROVED")
        self.assertEqual(got["reviews_after_push"], [["APPROVED", SHA[:7]]])
        self.assertEqual(got["protection_after_push"]["mergeStateStatus"], "BLOCKED")
        # The reviewer's approval is posted on the person's PR (the second PR number), between
        # the person's two pushes.
        order = [(r, m) for r, m, p, _ in fake.requests
                 if p.endswith("/pulls/12/reviews") or (r == "person" and m == "PUT")]
        self.assertEqual(order, [("person", "PUT"), ("reviewer", "POST"), ("person", "PUT")])

    def test_without_a_person_token_the_step_says_skipped(self):
        fake = FakeSpike()
        lines = run_quietly(fake)
        self.assertIn("skipped", step(lines, "stale_approval"))
        self.assertEqual(fake.mine("person"), [])

    def test_git_push_step_rechecks_the_decision(self):
        lines = run_quietly(FakeSpike())
        push = step(lines, "git_push")
        self.assertEqual(push["reviews_after_push"], [["APPROVED", SHA[:7]]])
        self.assertIn("mergeStateStatus", push["protection_after_push"])

    def test_main_reads_the_person_token_from_the_named_variable(self):
        seen = {}
        with mock.patch.object(spike, "run", side_effect=lambda s: seen.update(token=s.person_token)), \
                mock.patch.dict(spike.os.environ, {"MY_PAT": "secret"}):
            spike.main(["--repo", "o/r", "--keys", "/k", "--app", "reader=1", "--app", "reviewer=2",
                        "--app", "fixer=3", "--person-token-env", "MY_PAT"])
        self.assertEqual(seen["token"], "secret")


class RealAuth(spike.Spike):
    """The real ``gh`` dispatch; only the HTTP ``call`` and the App token mint are replaced."""

    def __init__(self, person_token):
        super().__init__("o/r", {"reader": 1, "reviewer": 2, "fixer": 3}, "/nowhere", "", person_token)
        self.auths: list = []

    def call(self, method, path, auth, body=None):
        self.auths.append(auth)
        return 200, {}

    def token(self, role):
        return "app-" + role


class PersonAuth(unittest.TestCase):
    def test_person_requests_use_the_persons_token_and_apps_use_theirs(self):
        s = RealAuth("pat")
        s.gh("person", "GET", "/x")
        s.gh("reviewer", "GET", "/x")
        self.assertEqual(s.auths, ["token pat", "token app-reviewer"])

    def test_main_defaults_to_spike_person_token(self):
        for env, want in (({"SPIKE_PERSON_TOKEN": "dflt"}, "dflt"), ({}, "")):
            with self.subTest(env=env):
                seen = {}
                with mock.patch.object(spike, "run", side_effect=lambda s: seen.update(t=s.person_token)), \
                        mock.patch.dict(spike.os.environ, env, clear=True):
                    spike.main(["--repo", "o/r", "--keys", "/k", "--app", "reader=1", "--app",
                                "reviewer=2", "--app", "fixer=3"])
                self.assertEqual(seen["t"], want)


class PostPushWait(unittest.TestCase):
    def test_git_push_waits_six_seconds_then_asks_until_settled(self):
        fake = FakeSpike()
        lines = run_quietly(fake)
        # GitHub answers UNKNOWN twice after the push: wait 6 s before each of three asks.
        tail = fake.events[fake.events.index(("git push",)) + 1:]
        self.assertEqual(tail[:6], [("sleep", 6), ("graphql", 11)] * 3)
        self.assertEqual(step(lines, "git_push")["protection_after_push"]["mergeStateStatus"], "BLOCKED")

    def test_stale_approval_waits_after_the_persons_push(self):
        fake = FakeSpike(person_token="pat")
        lines = run_quietly(fake)
        puts = [i for i, e in enumerate(fake.events) if e[:2] == ("person", "PUT")]
        tail = fake.events[puts[-1] + 1:]
        self.assertEqual(tail[:6], [("sleep", 6), ("graphql", 12)] * 3)
        self.assertEqual(step(lines, "stale_approval")["protection_after_push"]["mergeStateStatus"],
                         "BLOCKED")

    def test_a_late_github_is_printed_as_unsettled_after_the_bounded_asks(self):
        fake = FakeSpike()
        fake.unknown_left = 99
        with mock.patch.object(spike.time, "sleep") as sleep:
            got = fake.settled_protection(11)
        self.assertEqual(got["mergeStateStatus"], "UNKNOWN")
        self.assertEqual(sleep.call_args_list, [mock.call(6)] * 5)


class CheckSpike(FakeSpike):
    """``required_check`` end to end: GitHub's merge-state answers are scripted, in order."""

    def __init__(self, answers):
        super().__init__(check_branch="gated")
        self.answers, self.asked = list(answers), 0

    def graphql(self, query, variables):
        self.asked += 1
        got = self.answers.pop(0) if self.answers else "CLEAN"
        if got is None:  # GitHub returned no data at all
            return None
        return {"repository": {"pullRequest": {"mergeStateStatus": got}}}

    def gh(self, role, method, path, body=None):
        if method == "POST" and path.endswith("/check-runs"):
            return 201, {"id": 77}
        return super().gh(role, method, path, body)


class RequiredCheckSettling(unittest.TestCase):
    def run_check(self, answers):
        fake, out = CheckSpike(answers), io.StringIO()
        with mock.patch.object(spike.time, "sleep") as sleep, contextlib.redirect_stdout(out):
            spike.required_check(fake, "t")
        return fake, sleep, json.loads(out.getvalue())

    def test_a_missing_answer_is_asked_again_until_a_state_settles(self):
        fake, sleep, line = self.run_check([None, "UNKNOWN", "BLOCKED"])
        self.assertEqual(line["no_check"], "BLOCKED")  # not None from the first, empty read
        # three asks for the first state, one for each of the five others
        self.assertEqual(fake.asked, 3 + 5)
        self.assertEqual(line["success"], "CLEAN")

    def test_answers_that_never_arrive_are_printed_as_none_after_ten_asks_each(self):
        fake, sleep, line = self.run_check([None] * 100)
        self.assertEqual(fake.asked, 6 * 10)
        self.assertIsNone(line["no_check"])
        self.assertEqual(sleep.call_args_list.count(mock.call(6)), 60)


class SecondBranch(unittest.TestCase):
    def test_check_branch_runs_step_q9_only_when_given(self):
        for branch, calls in (("gated", 1), ("", 0)):
            with self.subTest(branch=branch), mock.patch.object(spike, "required_check") as rc:
                run_quietly(FakeSpike(check_branch=branch))
            self.assertEqual(rc.call_count, calls)


class DocsAgree(unittest.TestCase):
    def test_docs_name_the_flags_the_harness_has(self):
        doc = (ROOT / "docs" / "github-app-support.md").read_text()
        parser_src = (ROOT / "tools" / "github_app_spike.py").read_text()
        for flag in ("--person-token-env", "--check-branch"):
            self.assertIn(flag, doc)
            self.assertIn(flag, parser_src)


if __name__ == "__main__":
    unittest.main()
