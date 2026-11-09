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
            # The reviewer bot (id 22) and someone else (id 9): only the bot's review counts.
            return 200, [{"state": "APPROVED", "commit_id": SHA, "user": {"id": 22}},
                         {"state": "CHANGES_REQUESTED", "commit_id": "f" * 40, "user": {"id": 9}}]
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


def run_quietly(fake, entry=None):
    """The lines ``entry`` prints (default: ``spike.run(fake)``) with git and sleeping stubbed."""
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
        (entry or (lambda: spike.run(fake)))()
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

    def test_reviews_after_push_holds_only_the_reviewer_apps_reviews(self):
        """The fake's reviews list has the bot's approval and another user's review: the printed
        ``reviews_after_push`` means 'the App's approval', so only the bot's is kept."""
        for fake, name in ((FakeSpike(person_token="pat"), "stale_approval"),
                           (FakeSpike(), "git_push")):
            with self.subTest(step=name):
                got = step(run_quietly(fake), name)["reviews_after_push"]
                self.assertEqual(got, [["APPROVED", SHA[:7]]])

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


class AppArgs(unittest.TestCase):
    """``--app ROLE=ID`` is checked by argparse: a malformed value is a usage error (exit 2)."""

    def test_main_refuses_a_malformed_app_value_as_a_usage_error(self):
        good = ["--app", "reader=1", "--app", "reviewer=2", "--app", "fixer=3"]
        for bad in ("reader", "reader=", "=1", "reader=abc", "reader=1.5", "reader=-1"):
            with self.subTest(app=bad), mock.patch.object(spike, "run") as ran, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    spike.main(["--repo", "o/r", "--keys", "/k", "--app", bad] + good[2:])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("usage:", err.getvalue())
                self.assertIn("ROLE=ID", err.getvalue())
                ran.assert_not_called()

    def test_main_refuses_a_non_ascii_digit_id_as_a_usage_error(self):
        """A GitHub App ID is ASCII digits. ``int()`` would take Arabic-Indic ``١`` as 1, and
        ``str.isdigit()`` is true for ``²`` though ``int()`` refuses it; each must get the
        ``is not ROLE=ID`` usage error, not be accepted and not fail with a generic message."""
        for bad in ("reader=\u0661", "reader=\u00b2", "reader=\uff11"):
            with self.subTest(app=bad), mock.patch.object(spike, "run") as ran, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as cm:
                    spike.main(["--repo", "o/r", "--keys", "/k", "--app", bad,
                                "--app", "reviewer=2", "--app", "fixer=3"])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("is not ROLE=ID", err.getvalue())
                ran.assert_not_called()

    def test_main_still_refuses_a_missing_role_as_a_usage_error(self):
        with mock.patch.object(spike, "run") as ran, contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                spike.main(["--repo", "o/r", "--keys", "/k", "--app", "reader=1"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("exactly reader, reviewer and fixer", err.getvalue())
        ran.assert_not_called()

    def test_main_passes_numeric_app_ids_by_role(self):
        seen = {}
        with mock.patch.object(spike, "run", side_effect=lambda s: seen.update(apps=s.apps)):
            spike.main(["--repo", "o/r", "--keys", "/k", "--app", "reader=1", "--app", "reviewer=2",
                        "--app", "fixer=3"])
        self.assertEqual(seen["apps"], {"reader": 1, "reviewer": 2, "fixer": 3})


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

    def test_stale_approval_waits_for_a_settled_state_after_the_approval(self):
        """The pre-push read (``protection_after_approve``) is the observation row 6 compares
        against: an UNKNOWN right after the App's APPROVE must be asked again, not printed."""
        fake = ApprovalUnknown(person_token="pat")
        lines = run_quietly(fake)
        approve = fake.events.index(("reviewer", "POST", "/repos/o/r/pulls/12/reviews"))
        tail = fake.events[approve + 1:]
        self.assertEqual(tail[:6], [("sleep", spike.POST_PUSH_WAIT), ("graphql", 12)] * 3)
        got = step(lines, "stale_approval")["protection_after_approve"]
        self.assertEqual(got["mergeStateStatus"], "BLOCKED")

    def test_a_late_github_is_printed_as_unsettled_after_the_bounded_asks(self):
        fake = FakeSpike()
        fake.unknown_left = 99
        with mock.patch.object(spike.time, "sleep") as sleep:
            got = fake.settled_protection(11)
        self.assertEqual(got["mergeStateStatus"], "UNKNOWN")
        self.assertEqual(sleep.call_args_list, [mock.call(6)] * 5)


class ApprovalUnknown(FakeSpike):
    """GitHub answers UNKNOWN twice right after the reviewer App approves the person's PR."""

    def gh(self, role, method, path, body=None):
        got = super().gh(role, method, path, body)
        if (role, method) == ("reviewer", "POST") and path.endswith("/pulls/12/reviews"):
            self.unknown_left = 2
        return got


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


class DeliverySpike(FakeSpike):
    """Answers the webhook-delivery reads: ``list_status`` for the list, ``detail_status`` for each."""

    def __init__(self, list_status=200, detail_status=200, items=()):
        super().__init__()
        self.list_status, self.detail_status, self.items = list_status, detail_status, list(items)

    def app(self, role, method, path, body=None):
        if path.startswith("/app/hook/deliveries?"):
            return self.list_status, self.items if self.list_status == 200 else {"message": "denied"}
        if path.startswith("/app/hook/deliveries/"):
            if self.detail_status != 200:
                return self.detail_status, {"message": "gone"}
            return 200, {"request": {"payload": {"sender": {"login": "fixer-app[bot]", "type": "Bot"}}}}
        return super().app(role, method, path, body)


DELIVERY_STEPS = ("deliveries", "deliveries_after_reviewer_requests", "review_deliveries")
RECENT = {"id": 7, "event": "pull_request", "action": "opened", "delivered_at": "2999-01-01T00:00:00Z"}


class FailedDeliveryRead(unittest.TestCase):
    def delivery_lines(self, fake):
        return [line for line in run_quietly(fake) if line["step"] in DELIVERY_STEPS]

    def test_a_failed_list_read_is_not_printed_as_no_events(self):
        lines = self.delivery_lines(DeliverySpike(list_status=403))
        self.assertEqual(len(lines), 5, "three Apps, then the two reader reads")
        for line in lines:
            with self.subTest(step=line["step"], role=line.get("role")):
                self.assertEqual(line["read_status"], 403)
                events = [v for k, v in line.items() if k in ("since_start", "reader")]
                self.assertEqual(events, [None], "null, not [], so it cannot read as 'no events'")

    def test_an_empty_but_successful_read_stays_an_empty_list(self):
        lines = self.delivery_lines(DeliverySpike())
        self.assertEqual(len(lines), 5)
        for line in lines:
            with self.subTest(step=line["step"], role=line.get("role")):
                self.assertEqual(line["read_status"], 200)
                self.assertEqual([v for k, v in line.items() if k in ("since_start", "reader")], [[]])

    def test_each_role_line_names_its_own_read_status(self):
        lines = self.delivery_lines(DeliverySpike(items=[RECENT]))
        roles = [line["role"] for line in lines if line["step"] == "deliveries"]
        self.assertEqual(roles, list(spike.ROLES))
        self.assertEqual(lines[0]["since_start"][0]["event"], "pull_request")
        self.assertEqual(lines[0]["since_start"][0]["detail_status"], 200)

    def test_a_failed_payload_read_is_marked_on_the_event(self):
        lines = self.delivery_lines(DeliverySpike(detail_status=404, items=[RECENT]))
        event = lines[0]["since_start"][0]
        self.assertEqual(lines[0]["read_status"], 200)
        self.assertEqual(event["detail_status"], 404)
        self.assertIsNone(event["sender"], "the payload was not read: say so, not that it had none")
        # #1894: not "" and False, which read as a measured "no commit" and "no installation".
        self.assertIsNone(event["review_commit"])
        self.assertIsNone(event["installation"])


class Refusing(FakeSpike):
    """``FakeSpike`` where chosen requests get a failure answer instead of the usual one.

    ``refuse`` is a list of ``(role, method, path part, answer[, let_through])``: a request of that
    role and method whose path holds the part gets ``answer`` once ``let_through`` earlier matches
    have been answered as usual. ``app_answer`` replaces the answer to ``GET /app``."""

    def __init__(self, refuse=(), app_answer=None, **kwargs):
        super().__init__(**kwargs)
        self.refuse, self.app_answer, self.seen = list(refuse), app_answer, {}

    def app(self, role, method, path, body=None):
        if path == "/app" and self.app_answer:
            return self.app_answer
        return super().app(role, method, path, body)

    def gh(self, role, method, path, body=None):
        for i, (r, m, part, answer, *skip) in enumerate(self.refuse):
            if (r, m) == (role, method) and part in path:
                self.seen[i] = self.seen.get(i, 0) + 1
                if self.seen[i] > (skip[0] if skip else 0):
                    self.requests.append((role, method, path, body))
                    return answer
        return super().gh(role, method, path, body)


def run_main(fake):
    """The harness's ``main`` on ``fake``: ``(exit code, the JSON lines it printed)``."""
    argv = ["--repo", "o/r", "--keys", "/k", "--app", "reader=1", "--app", "reviewer=2",
            "--app", "fixer=3"]
    if fake.check_branch:
        argv += ["--check-branch", fake.check_branch]
    code = []
    with mock.patch.object(spike, "Spike", return_value=fake):
        lines = run_quietly(fake, lambda: code.append(spike.main(argv)))
    return code[0], lines


NOT_FOUND = (404, {"message": "Not Found", "token": "ghs_must_not_be_printed"})
UNPROCESSABLE = (422, {"message": "Reference already exists"})
FORBIDDEN = (403, {"message": "Resource not accessible by integration"})


class FailedStatuses(unittest.TestCase):
    """A step whose answer the run cannot go on without ends it with one named JSON line (#1171)."""

    CASES = (
        # name, fake arguments, step, status, request that must never be made afterwards
        ("app 401", dict(app_answer=(401, {"message": "A JSON web token could not be decoded"})),
         "identity", 401, None),
        ("app answer without a slug", dict(app_answer=(200, {})), "identity", 200, None),
        ("app answer that is not an object", dict(app_answer=(200, None)), "identity", 200, None),
        ("main ref missing", dict(refuse=[("fixer", "GET", "/git/ref/heads/main", NOT_FOUND)]),
         "branch", 404, "pulls"),
        ("branch not created", dict(refuse=[("fixer", "POST", "/git/refs", UNPROCESSABLE)]),
         "branch", 422, "pulls"),
        ("app.py missing on the branch",
         dict(refuse=[("fixer", "GET", "/contents/app.py", NOT_FOUND)]), "put_file", 404, "pulls"),
        ("commit refused", dict(refuse=[("fixer", "PUT", "/contents/app.py", FORBIDDEN)]),
         "put_file", 403, "pulls"),
        ("commit answer without a sha",
         dict(refuse=[("fixer", "PUT", "/contents/app.py", (200, {"content": {}}))]),
         "put_file", 200, "pulls"),
        ("pull request refused", dict(refuse=[("fixer", "POST", "/pulls", FORBIDDEN)]),
         "open_pr", 403, "requested_reviewers"),
        ("commit lookup 404", dict(refuse=[("reader", "GET", "/commits/", NOT_FOUND)]),
         "fixer_pr", 404, "requested_reviewers"),
        ("pushed commit lookup 404", dict(refuse=[("reader", "GET", "/commits/", NOT_FOUND, 1)]),
         "git_push", 404, "/merge"),
        ("fixer bot user lookup 404", dict(refuse=[("fixer", "GET", "/users/", NOT_FOUND, 1)]),
         "git_push", 404, "/merge"),
        ("reviewer cannot read app.py",
         dict(refuse=[("reviewer", "GET", "/contents/app.py", NOT_FOUND)]),
         "reviewer_permissions", 404, "/merge"),
        ("person's branch refused",
         dict(person_token="pat", refuse=[("person", "POST", "/git/refs", FORBIDDEN)]),
         "branch", 403, "/pulls/12/reviews"),
        ("person's pull request refused",
         dict(person_token="pat", refuse=[("person", "POST", "/pulls", FORBIDDEN)]),
         "open_pr", 403, "/pulls/12/reviews"),
        ("check branch missing",
         dict(check_branch="gated", refuse=[("fixer", "GET", "/git/ref/heads/gated", NOT_FOUND)]),
         "branch", 404, "/check-runs"),
        # the first creation (the fixer PR's branch) goes through; the check branch's is refused
        ("check branch not created",
         dict(check_branch="gated", refuse=[("fixer", "POST", "/git/refs", UNPROCESSABLE, 1)]),
         "branch", 422, "/check-runs"),
    )

    def test_main_ends_with_the_named_step_and_exits_1(self):
        for name, kwargs, step_name, status, _ in self.CASES:
            with self.subTest(name):
                code, lines = run_main(Refusing(**kwargs))
                self.assertEqual(code, 1)
                self.assertEqual(set(lines[-1]), {"step", "status", "error"})
                self.assertEqual((lines[-1]["step"], lines[-1]["status"]), (step_name, status))
                self.assertTrue(lines[-1]["error"])

    def test_the_run_stops_at_the_failed_step(self):
        for name, kwargs, _, _, never in self.CASES:
            if never is None:
                continue
            with self.subTest(name):
                fake = Refusing(**kwargs)
                run_main(fake)
                self.assertFalse([p for _, _, p, _ in fake.requests if never in p],
                                 f"the run went on to {never} after the failure")

    def test_a_refused_check_branch_opens_no_pull_request_and_posts_no_check_run(self):
        """``required_check`` creates its branch from ``--check-branch``: when that POST is refused
        the run names the branch step and goes no further (#1888 F2)."""
        fake = Refusing(check_branch="gated",
                        refuse=[("fixer", "POST", "/git/refs", UNPROCESSABLE, 1)])
        code, lines = run_main(fake)
        self.assertEqual(code, 1)
        self.assertEqual((lines[-1]["step"], lines[-1]["status"]), ("branch", 422))
        self.assertIn("spike-check-", lines[-1]["error"])
        self.assertNotIn("required_check", [line["step"] for line in lines])
        self.assertEqual([p for _, m, p, _ in fake.requests if m == "POST" and p.endswith("/pulls")],
                         ["/repos/o/r/pulls"], "only the fixer PR before the check branch")
        self.assertFalse([p for _, _, p, _ in fake.requests if "/check-runs" in p])

    def test_nothing_after_a_failed_identity_is_printed(self):
        code, lines = run_main(Refusing(app_answer=(401, {"message": "Bad credentials"})))
        self.assertEqual([line["step"] for line in lines], ["identity"])
        self.assertIn("reader GET /app", lines[0]["error"])
        self.assertIn("Bad credentials", lines[0]["error"])

    def test_the_error_holds_github_message_and_never_the_rest_of_the_body(self):
        _, lines = run_main(Refusing(refuse=[("fixer", "GET", "/git/ref/heads/main", NOT_FOUND)]))
        self.assertIn("Not Found", lines[-1]["error"])
        self.assertNotIn("ghs_must_not_be_printed", json.dumps(lines))

    def test_a_text_answer_is_kept_short(self):
        _, lines = run_main(Refusing(app_answer=(502, "x" * 5000)))
        self.assertLessEqual(len(lines[-1]["error"]), spike.ERROR_LIMIT)

    def test_a_run_that_gets_every_answer_still_exits_0(self):
        code, lines = run_main(FakeSpike())
        self.assertEqual(code, 0)
        self.assertNotIn("error", lines[-1])

    def test_run_raises_step_failed_with_the_step_and_status(self):
        with self.assertRaises(spike.StepFailed) as ctx:
            run_quietly(Refusing(refuse=[("fixer", "GET", "/contents/app.py", NOT_FOUND)]))
        self.assertEqual((ctx.exception.step, ctx.exception.status), ("put_file", 404))


class NoCredentialInTheError(unittest.TestCase):
    """What GitHub (or a proxy in front of it) says is printed, so a credential in it must not be
    (#1888 F1): the docs promise that no token reaches the log."""

    HELD = "Zq9-held-secret-value-0123456"  # an installation token with no prefix to recognise
    SHAPES = (
        ("ghs_ token", "bad token ghs_secret_example", "ghs_secret_example"),
        ("short ghp_ token", "rejected ghp_abc", "ghp_abc"),
        ("fine-grained token", "github_pat_11ABCDEFG_zzzzzzzzzz is expired", "github_pat_11ABCDEFG"),
        ("Authorization value", "sent Authorization: token 0123456789abcdef0123456789abcdef01234567",
         "0123456789abcdef0123456789abcdef01234567"),
        ("bearer value", "Bearer abcdefghijklmnopqrstuvwxyz012345 is not valid",
         "abcdefghijklmnopqrstuvwxyz012345"),
        ("short bearer value", "rejected Bearer abc123xyz", "abc123xyz"),
        ("short bearer value in capitals", "rejected BEARER ab12", "ab12"),
        ("short Authorization token", "sent Authorization: token abc123", "abc123"),
        ("short Authorization bearer", "sent Authorization: Bearer abc123xyz", "abc123xyz"),
        ("Authorization value with no scheme", "sent authorization=s3cr3t;", "s3cr3t"),
        ("quoted Authorization header", 'headers {"Authorization": "Bearer s3cr3t"}', "s3cr3t"),
        ("short token value with a digit", "bad token abc123xyz", "abc123xyz"),
        ("app JWT", "bad jwt eyJhbGciOiJSUzI1NiJ9.eyJpYXQiOjE2MDAwMDAwMDB9.c2lnbmF0dXJlLWJ5dGVz",
         "eyJhbGciOiJSUzI1NiJ9"),
        ("private key", "key -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
         "-----END RSA PRIVATE KEY----- rejected", "MIIEowIBAAKCAQEA"),
    )

    def test_a_credential_shaped_message_or_text_answer_is_redacted(self):
        for name, said, secret in self.SHAPES:
            for kind, answer in (("message", (401, {"message": said})), ("text", (502, said))):
                with self.subTest(f"{name} as {kind}"):
                    code, lines = run_main(Refusing(app_answer=answer))
                    self.assertEqual(code, 1)
                    self.assertNotIn(secret, json.dumps(lines))
                    self.assertIn("[REDACTED]", lines[-1]["error"])
                    self.assertEqual((lines[-1]["step"], lines[-1]["status"]),
                                     ("identity", answer[0]))

    def test_the_text_around_a_credential_is_kept(self):
        _, lines = run_main(Refusing(app_answer=(401, {"message": "bad token ghs_secret_example"})))
        self.assertIn("bad token [REDACTED]", lines[-1]["error"])

    def test_plain_words_around_token_and_authorization_are_kept(self):
        """The Authorization/Bearer/token patterns must not eat ordinary sentences."""
        said = "A JSON web token could not be decoded; Authorization failed for this request"
        _, lines = run_main(Refusing(app_answer=(401, {"message": said})))
        self.assertEqual(lines[-1]["error"], f"reader GET /app: {said}")

    def test_a_token_this_run_holds_is_redacted_whatever_it_looks_like(self):
        fake = Refusing(app_answer=(401, {"message": f"Bad credentials: {self.HELD}"}))
        fake.tokens["reader"]["token"] = self.HELD
        _, lines = run_main(fake)
        self.assertNotIn(self.HELD, json.dumps(lines))
        self.assertIn("Bad credentials: [REDACTED]", lines[-1]["error"])

    def test_the_person_token_is_redacted_too(self):
        person = "0123456789abcdef0123456789abcdef01234567"  # a classic token has no prefix
        fake = Refusing(person_token=person, refuse=[
            ("person", "POST", "/git/refs", (403, {"message": f"no access for {person}"}))])
        _, lines = run_main(fake)
        self.assertEqual((lines[-1]["step"], lines[-1]["status"]), ("branch", 403))
        self.assertNotIn(person, json.dumps(lines))

    def test_a_cut_cannot_leave_the_start_of_a_held_token(self):
        """The token is removed before the error is cut to ``ERROR_LIMIT``: cutting first would
        leave its first characters in the log, where no whole-token match can find them."""
        lead = len("reader GET /app: ") + 1
        said = "x" * (spike.ERROR_LIMIT - lead - 10) + " " + self.HELD
        fake = Refusing(app_answer=(502, said))
        fake.tokens["reader"]["token"] = self.HELD
        _, lines = run_main(fake)
        self.assertNotIn(self.HELD[:6], json.dumps(lines))
        self.assertLessEqual(len(lines[-1]["error"]), spike.ERROR_LIMIT)

    def test_scrub_replaces_every_secret_given_longest_first(self):
        self.assertEqual(spike.scrub("a abcd-long b abcd c", ["abcd", "abcd-long", "x", None]),
                         "a [REDACTED] b [REDACTED] c")


class DocsAgree(unittest.TestCase):
    def test_docs_say_a_failed_delivery_read_is_null_with_its_status(self):
        doc = (ROOT / "docs" / "github-app-support.md").read_text()
        for word in ("read_status", "detail_status"):
            self.assertIn(word, doc)

    def test_q3_comment_claims_no_human_request_the_loop_does_not_make(self):
        src = (ROOT / "tools" / "github_app_spike.py").read_text()
        q3 = next(line for line in src.splitlines() if "# Q3:" in line)
        self.assertNotIn("human for contrast", q3)
        self.assertNotIn("human for contrast", src)

    def test_docs_say_the_persons_row_6_change_is_a_contents_api_commit(self):
        doc = (ROOT / "docs" / "github-app-support.md").read_text()
        flat = " ".join(doc.split())
        self.assertNotIn("pushes as the person", flat)
        self.assertIn("commits to the branch as that person (contents API)", flat)
        self.assertIn("The person's `git push` is not repeated", flat)

    def test_docs_name_the_flags_the_harness_has(self):
        doc = (ROOT / "docs" / "github-app-support.md").read_text()
        parser_src = (ROOT / "tools" / "github_app_spike.py").read_text()
        for flag in ("--person-token-env", "--check-branch"):
            self.assertIn(flag, doc)
            self.assertIn(flag, parser_src)


if __name__ == "__main__":
    unittest.main()
