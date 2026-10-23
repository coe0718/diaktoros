"""#1163 M2c: the base's protection for the ``diaktoros/review`` merge check — what doctor reads
and says, what ``hermes dk protect`` and setup write (only the loop's own ruleset), and what
``ready`` says about a blocked PR in App mode."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import ci, config, doctor, gh, protection  # noqa: E402
from diaktoros.cli import checks as cli_checks, install as cli_install  # noqa: E402

APP = 5416187


def app_loop(**extra) -> dict:
    return {"id": "w", "repo": "acme/w", "base": "main", "reviewer_seat": "r[bot]",
            "apps": {"r[bot]": {"app_id": APP, "key_file": "/k"}}, "tokens": {"reader": "/t"},
            "read_token": "reader", **extra}


def pat_loop(**extra) -> dict:
    return {"id": "w", "repo": "acme/w", "base": "main", "reviewer_seat": "rev",
            "apps": {}, "tokens": {"rev": "/t", "reader": "/t"}, "read_token": "reader", **extra}


def requires(*sources, approvals=0) -> list:
    """A ruleset answer: the merge check from each source (None = any), plus approvals."""
    checks = [{"context": ci.MERGE_CHECK, **({"integration_id": s} if s else {})} for s in sources]
    rules = [{"type": "required_status_checks",
              "parameters": {"required_status_checks": [{"context": "tests"}] + checks}}]
    if approvals:
        rules.append({"type": "pull_request",
                      "parameters": {"required_approving_review_count": approvals}})
    return rules


class Fake(unittest.TestCase):
    """The faked GitHub only (no tests): rulesets, classic protection, the ruleset list."""

    def setUp(self):
        self.rules, self.classic, self.listed = [], None, []
        self.write_error = ""
        self.calls = []

        def request(loop, path, method="GET", body=None, login=None):
            self.calls.append((method, path, body, login))
            if method != "GET":
                if self.write_error:
                    return gh.Response(None, self.write_error, 403, {})
                return gh.Response({} if method == "DELETE" else {"id": 77}, "", 200, {})
            if "/rules/branches/" in path:
                return (gh.Response(None, "HTTP 403", 403, {}) if self.rules is None else
                        gh.Response(self.rules, "", 200, {}))
            if path.endswith("/protection"):
                if self.classic is None:
                    return gh.Response(None, "HTTP 404 Branch not protected", 404, {})
                if self.classic == "forbidden":
                    return gh.Response(None, "HTTP 403", 403, {})
                return gh.Response(self.classic, "", 200, {})
            if "/rulesets" in path:
                return (gh.Response(None, "HTTP 403", 403, {}) if self.listed is None else
                        gh.Response(self.listed, "", 200, {}))
            return gh.Response(None, "HTTP 404", 404, {})
        patch = mock.patch.object(gh, "request", side_effect=request)
        patch.start()
        self.addCleanup(patch.stop)

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class Read(Fake):
    def test_rulesets_and_classic_protection_are_both_read(self):
        self.rules = requires(APP, None, approvals=2)
        self.classic = {"required_status_checks": {"contexts": [ci.MERGE_CHECK]},
                        "required_pull_request_reviews": {"required_approving_review_count": 3}}
        found = protection.read(app_loop())
        self.assertEqual((sorted(found.sources, key=str), found.approvals, found.unread),
                         ([APP, None, None], 3, []))   # ruleset any-source + classic
        self.classic = {"required_status_checks": {
            "contexts": [ci.MERGE_CHECK], "checks": [{"context": ci.MERGE_CHECK, "app_id": 9}]}}
        self.rules = []
        self.assertEqual(protection.read(app_loop()).sources, [9])     # a listed check, not twice

    def test_unread_answers_are_kept_apart_from_an_unprotected_base(self):
        self.assertTrue(protection.read(app_loop()).known)             # 404s: nothing required
        self.rules, self.classic = None, "forbidden"
        found = protection.read(app_loop())
        self.assertFalse(found.known)
        self.assertEqual(len(found.unread), 2)
        self.classic = None
        self.assertTrue(protection.read(app_loop()).known)             # one answer is enough


class Doctor(Fake):
    def check(self, loop, offline=False):
        return doctor.check_merge_check(loop, offline)

    def test_a_loop_that_does_not_post_the_check_says_nothing_unless_the_base_needs_it(self):
        self.assertIsNone(self.check(pat_loop()))
        self.assertIsNone(self.check(pat_loop(), offline=True))
        self.rules = requires(None)
        c = self.check(pat_loop())
        self.assertEqual(c.status, doctor.MISMATCH)
        self.assertIn("every PR stays blocked", c.detail)
        self.assertIn("--merge-check on", c.fix)
        self.assertIn("--remove", c.fix)
        self.assertEqual(self.check(app_loop(merge_check="off")).status, doctor.MISMATCH)

    def test_app_mode_wants_the_check_required_and_pinned_to_the_reviewer_app(self):
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertIn("does not require it", c.detail)
        self.assertIn("hermes dk protect --loop w", c.fix)
        self.rules = requires(None)
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertIn("any source", c.detail)
        self.assertIn("hermes dk protect", c.fix)
        self.rules = requires(APP)
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.VERIFIED)
        self.assertIn(f"reviewer App ({APP})", c.detail)
        self.rules = requires(APP, 42)
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.MISMATCH)
        self.assertIn("App 42", c.detail)
        self.assertIn("never be satisfied", c.detail)

    def test_app_mode_flags_required_approvals(self):
        self.rules = requires(APP, approvals=1)
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertIn("person's approval", c.detail)
        self.assertIn("turn required approvals off", c.fix)
        self.assertNotIn("hermes dk protect", c.fix)                 # the check itself is fine
        self.rules = requires(None, approvals=1)
        self.assertEqual(self.check(pat_loop(merge_check="on")).status, doctor.VERIFIED)  # a PAT's count

    def test_a_pat_loop_that_opted_in(self):
        self.assertEqual(self.check(pat_loop(merge_check="on")).status, doctor.UNKNOWN)
        self.rules = requires(None)
        c = self.check(pat_loop(merge_check="on"))
        self.assertEqual(c.status, doctor.VERIFIED)
        self.assertIn("cannot be pinned", c.detail)

    def test_offline_and_unreadable(self):
        self.assertEqual(self.check(app_loop(), offline=True).status, doctor.UNKNOWN)
        self.assertEqual(self.calls, [])
        self.rules, self.classic = None, "forbidden"
        c = self.check(app_loop())
        self.assertEqual(c.status, doctor.UNKNOWN)
        self.assertIn("could not read", c.detail)
        self.assertIsNone(self.check(pat_loop()))

    def test_branch_protection_does_not_count_the_loops_own_check(self):
        loop = {**pat_loop(), "required_checks": ["tests"]}
        self.rules = requires(None)
        self.assertIsNone(doctor.check_branch_protection(loop, False))

    def test_doctor_runs_it(self):
        with mock.patch.object(doctor, "check_merge_check",
                               return_value=doctor.Check("merge-check", doctor.VERIFIED, "x")) as m, \
                mock.patch.object(doctor, "check_token_expiry_scopes", return_value=[]), \
                mock.patch.object(doctor, "check_labels", return_value=None), \
                mock.patch.object(doctor, "check_branch_protection", return_value=None):
            names = [c.name for c in doctor.check_environment(app_loop(), False)]
        m.assert_called_once()
        self.assertIn("merge-check", names)


class Write(Fake):
    def test_the_ruleset_requires_the_check_pinned_to_the_reviewer_app_and_nothing_else(self):
        body = protection.ruleset_body(app_loop())
        self.assertEqual((body["name"], body["enforcement"], body["bypass_actors"]),
                         (ci.MERGE_CHECK, "active", []))
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        [rule] = body["rules"]
        self.assertEqual(rule["parameters"]["required_status_checks"],
                         [{"context": ci.MERGE_CHECK, "integration_id": APP}])
        self.assertEqual(protection.ruleset_body(pat_loop(merge_check="on"))["rules"][0]
                         ["parameters"]["required_status_checks"], [{"context": ci.MERGE_CHECK}])

    def test_require_creates_then_updates_its_own_ruleset(self):
        done, line = protection.require(app_loop(), "admin")
        self.assertTrue(done, line)
        [(method, path, body, login)] = self.writes()
        self.assertEqual((method, path, login), ("POST", "/repos/acme/w/rulesets", "admin"))
        self.assertIn("created", line)
        self.listed = [{"id": 3, "name": "someone else's"}, {"id": 5, "name": ci.MERGE_CHECK}]
        self.calls.clear()
        done, line = protection.require(app_loop(), "admin")
        self.assertEqual([c[:2] for c in self.writes()], [("PUT", "/repos/acme/w/rulesets/5")])
        self.assertIn("updated", line)

    def test_a_failed_read_or_write_is_reported_not_guessed(self):
        self.listed = None
        done, line = protection.require(app_loop(), "admin")
        self.assertFalse(done)
        self.assertEqual(self.writes(), [])                            # never a blind POST
        self.listed, self.write_error = [], "HTTP 403 Resource not accessible"
        done, line = protection.require(app_loop(), "admin")
        self.assertFalse(done)
        self.assertIn("admin access", line)

    def test_remove_deletes_only_the_loops_own_ruleset(self):
        done, line = protection.remove(app_loop(), "admin")
        self.assertTrue(done)
        self.assertEqual(self.writes(), [])
        self.assertIn("nothing to remove", line)
        self.listed = [{"id": 3, "name": "main protection"}, {"id": 5, "name": ci.MERGE_CHECK}]
        done, line = protection.remove(app_loop(), "admin")
        self.assertEqual([c[:2] for c in self.writes()], [("DELETE", "/repos/acme/w/rulesets/5")])


class Command(Fake):
    def run_cmd(self, loop, *flags):
        args = argparse.Namespace(loop="w", admin_token="admin", remove="--remove" in flags,
                                  dry_run="--dry-run" in flags)
        out = io.StringIO()
        with mock.patch.object(config, "load_id", return_value=loop), \
                contextlib.redirect_stdout(out):
            rc = cli_checks.cmd_protect(args)
        return rc, out.getvalue()

    def test_refuses_a_loop_that_does_not_post_the_check(self):
        rc, out = self.run_cmd(pat_loop())
        self.assertEqual(rc, 2)
        self.assertIn("--merge-check on", out)
        self.assertEqual(self.calls, [])
        rc, out = self.run_cmd(pat_loop(), "--remove")             # removing is always allowed
        self.assertEqual(rc, 0, out)

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_cmd(app_loop(), "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(str(APP), out)
        self.assertEqual(self.calls, [])

    def test_success_names_the_approvals_left_and_the_way_back(self):
        self.rules = requires(approvals=1)
        rc, out = self.run_cmd(app_loop())
        self.assertEqual(rc, 0, out)
        self.assertIn("still requires 1 approving review", out)
        self.assertIn("--remove", out)
        self.write_error = "HTTP 403"
        self.assertEqual(self.run_cmd(app_loop())[0], 1)

    def test_the_command_is_registered(self):
        import run_tests as t
        parsed = t.parser_for({}).parse_args(["protect", "--loop", "w", "--remove"])
        self.assertIs(parsed.func, cli_checks.cmd_protect)


class Setup(Fake):
    def step(self, loop, protect=False, interactive=False, answer="n"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("builtins.input", return_value=answer):
            cli_install._setup_merge_check(argparse.Namespace(protect=protect), loop, "admin",
                                           interactive)
        return out.getvalue()

    def test_nothing_for_a_loop_that_does_not_post_the_check(self):
        self.assertEqual(self.step(pat_loop(), protect=True), "")
        self.assertEqual(self.calls, [])

    def test_an_already_required_check_is_left_alone(self):
        self.rules = requires(APP)
        self.assertIn("already requires", self.step(app_loop(), protect=True))
        self.assertEqual(self.writes(), [])

    def test_declined_or_unattended_prints_the_command_and_writes_nothing(self):
        for kw in ({}, {"interactive": True, "answer": "n"}):
            with self.subTest(kw):
                out = self.step(app_loop(), **kw)
                self.assertIn("hermes dk protect --loop w --admin-token admin", out)
        self.assertEqual(self.writes(), [])

    def test_protect_or_yes_writes_the_ruleset_and_warns_about_approvals(self):
        self.rules = requires(approvals=2)
        out = self.step(app_loop(), protect=True)
        self.assertEqual([c[0] for c in self.writes()], ["POST"])
        self.assertIn("still requires 2 approving review", out)
        self.calls.clear()
        self.step(app_loop(), interactive=True, answer="y")
        self.assertEqual([c[0] for c in self.writes()], ["POST"])

    def test_setup_takes_protect(self):
        import run_tests as t
        parsed = t.parser_for({}).parse_args(["setup", "--repo", "acme/w", "--protect"])
        self.assertTrue(parsed.protect)


class ReadyHint(unittest.TestCase):
    def test_a_blocked_pr_in_app_mode_names_the_approvals(self):
        import test_merge_check as tmc
        base = tmc.KindsBase("setUp")
        base.setUp()
        self.addCleanup(base.doCleanups)
        blocked = {**tmc.CLEAN, "mergeable_state": "blocked"}
        [text] = [t for _, t in base.kinds(detail=blocked) if "blocked" in t]
        self.assertNotIn("GitHub App", text)
        base.loop.update(reviewer_seat="rev[bot]", apps={"rev[bot]": {"app_id": 1, "key_file": "/k"}})
        [text] = [t for _, t in base.kinds(detail=blocked) if "blocked" in t]
        self.assertIn("approvals GitHub never counts", text)


if __name__ == "__main__":
    unittest.main()
