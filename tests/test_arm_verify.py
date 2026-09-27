"""`arm` reports what GitHub shows after the PATCH, and `init --schedule` fails when no job exists.

Issue #55 / #84: a refused PATCH used to print "hook 1 → paused" and exit 0; a failed
`cron create` printed an unquoted fallback and exited 0. Disposable config/state only.
"""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, gh

HOST = "https://gw.example"


def raw_loop(loop_id):
    return {"id": loop_id, "repo": f"owner/{loop_id}", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "read_token": "reader", "host": HOST,
            "seats": {"reviewer": {"route": f"{loop_id}-review", "profile": "reviewer"},
                      "fixer": {"route": f"{loop_id}-fix", "profile": "fixer"}}}


class FakeGitHub:
    """Hooks by id; PATCH may be refused (403) or silently ignored."""

    def __init__(self, hooks, patch_error="", ignore_patch=False, list_error="", get_error=""):
        self.hooks = hooks
        self.patch_error = patch_error
        self.ignore_patch = ignore_patch
        self.list_error = list_error
        self.get_error = get_error
        self.calls = []

    def fetch(self, loop, path, method="GET", body=None, login=None):
        self.calls.append((method, path, login))
        if path.endswith("/hooks?per_page=100"):
            if self.list_error:
                return None, self.list_error
            return [dict(h) for h in self.hooks.values()], ""
        hook_id = int(path.rsplit("/", 1)[-1])
        if method == "PATCH":
            error = (self.patch_error.get(hook_id, "") if isinstance(self.patch_error, dict)
                     else self.patch_error)
            if error:
                return None, error
            if not self.ignore_patch:
                self.hooks[hook_id]["active"] = body["active"]
            return dict(self.hooks[hook_id]), ""
        if self.get_error:
            return None, self.get_error
        return dict(self.hooks[hook_id]), ""


def hooks(active):
    return {1: {"id": 1, "active": active,
                "config": {"url": f"{HOST}/p/reviewer/webhooks/widgets-review"}},
            2: {"id": 2, "active": active,
                "config": {"url": f"{HOST}/p/fixer/webhooks/widgets-fix"}},
            3: {"id": 3, "active": active,
                "config": {"url": "https://elsewhere.example/ci"}}}


class ArmVerifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs"),
                                      "HERMES_HOME": str(self.root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        config.config_dir().mkdir()
        (config.config_dir() / "widgets.json").write_text(json.dumps(raw_loop("widgets")))
        # The gateway's binding: every arm test runs against a real route registry, so the
        # predicate under test is the registry's URL, never one derived from the loop file.
        self.write_registry({"widgets-review": "reviewer", "widgets-fix": "fixer"})

    def write_registry(self, bindings):
        from review_loop import routes
        path = routes.subs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({name: {"profile": profile, "secret": "fixture-secret",
                                           "events": ["pull_request"], "prompt": "p",
                                           "host": HOST}
                                    for name, profile in bindings.items()}))

    def arm(self, fake, pause=False, admin_token="", loop="widgets"):
        args = argparse.Namespace(loop=loop, pause=pause, admin_token=admin_token)
        out = io.StringIO()
        with patch.object(gh, "fetch", fake.fetch), redirect_stdout(out):
            rc = cli.cmd_arm(args)
        return rc, out.getvalue()

    def test_refused_patch_reports_truth_and_fails(self):
        fake = FakeGitHub(hooks(True), patch_error="HTTP 403 Resource not accessible")
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("→ paused", out)
        self.assertIn("hook 1 is still active, not paused", out)
        self.assertIn("HTTP 403", out)
        self.assertEqual(out.count("--admin-token <owner login>"), 1, out)
        # On a user-owned repo the reader usually *is* the owner, so "--admin-token <owner>" alone
        # would name the same read-only file: the fix must say the reader's file needs hook write.
        self.assertIn("that is the reader's file", out)
        self.assertIn("if the reader is the owner give that file hook write", out)
        self.assertIn("`repository_hooks: write`", out)
        self.assertEqual([h["active"] for h in fake.hooks.values()], [True, True, True])
        self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/3", "reader"), fake.calls)

    def test_admin_token_names_that_login(self):
        fake = FakeGitHub(hooks(True), patch_error="HTTP 403")
        rc, out = self.arm(fake, pause=True, admin_token="owner")
        self.assertEqual(rc, 1)
        self.assertIn("'owner'", out)
        self.assertIn("check that login's token file", out)
        self.assertNotIn("reader's file", out)
        self.assertIn(("PATCH", "/repos/owner/widgets/hooks/1", "owner"), fake.calls)

    def test_accepted_but_unchanged_patch_fails(self):
        fake = FakeGitHub(hooks(True), ignore_patch=True)
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("read-back disagrees", out)

    def test_unreadable_read_back_fails(self):
        fake = FakeGitHub(hooks(False), get_error="HTTP 502")
        rc, out = self.arm(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT CONFIRMED active", out)
        self.assertIn("retry `arm`", out)

    def test_success_reports_read_back_state(self):
        fake = FakeGitHub(hooks(True))
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 1 → paused (read back)", out)
        self.assertIn("hook 2 → paused (read back)", out)
        self.assertTrue(fake.hooks[3]["active"])
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 1 already paused", out)

    def test_unreadable_listing_fails_with_fix(self):
        rc, out = self.arm(FakeGitHub(hooks(True), list_error="HTTP 404"))
        self.assertEqual(rc, 1, out)
        self.assertIn("could not read the repo's hooks", out)
        self.assertIn("--admin-token", out)

    def test_no_loop_hooks_fails(self):
        fake = FakeGitHub({3: hooks(True)[3]})
        rc, out = self.arm(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("no loop hooks found", out)

    def test_one_seat_hook_missing_fails_and_names_it(self):
        # Only the reviewer's hook exists: the loop would be armed halfway, which is not "armed".
        fake = FakeGitHub({1: hooks(False)[1], 3: hooks(False)[3]})
        rc, out = self.arm(fake)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook 1 → active (read back)", out)
        self.assertIn("hook:widgets-fix ABSENT (fixer seat)", out)
        self.assertNotIn("widgets-review ABSENT", out)
        self.assertIn("init --hooks", out)
        self.assertIn("doctor --loop widgets", out)
        self.assertIn("arm NOT confirmed for: widgets", out)
        # And the other way round, pausing with only the fixer's hook.
        fake = FakeGitHub({2: hooks(True)[2]})
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook:widgets-review ABSENT (reviewer seat)", out)
        self.assertNotIn("widgets-fix ABSENT", out)

    def test_hook_without_a_real_active_bool_is_not_already_in_state(self):
        # A listing entry with no `active` key is not proof the hook is paused: PATCH and read back.
        listing = hooks(True)
        del listing[1]["active"]
        listing[2]["active"] = "false"
        fake = FakeGitHub(listing)
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already paused", out)
        self.assertIn("hook 1 → paused (read back)", out)
        self.assertIn("hook 2 → paused (read back)", out)
        self.assertIn(("PATCH", "/repos/owner/widgets/hooks/1", "reader"), fake.calls)
        self.assertIn(("PATCH", "/repos/owner/widgets/hooks/2", "reader"), fake.calls)

    def test_transient_patch_failure_gets_the_retry_fix(self):
        fake = FakeGitHub(hooks(True), patch_error="HTTP 502 Bad Gateway")
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook 1 is still active, not paused: PATCH failed (HTTP 502", out)
        fix = [line for line in out.splitlines() if "fix:" in line]
        self.assertEqual(len(fix), 1, out)
        self.assertTrue(fix[0].startswith("[widgets] fix: hooks 1, 2: retry `arm`"), fix)

    def test_each_failed_hook_gets_its_own_fix(self):
        # One refusal must not hide the retry advice another hook's 5xx earned (and vice versa).
        fake = FakeGitHub(hooks(True), patch_error={1: "HTTP 403 Resource not accessible",
                                                    2: "HTTP 502 Bad Gateway"})
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        fix = [line for line in out.splitlines() if "fix:" in line]
        self.assertEqual(len(fix), 2, out)
        self.assertTrue(fix[0].startswith("[widgets] fix: hook 1: the token for 'reader' needs "
                                          "hook write"), fix)
        self.assertTrue(fix[1].startswith("[widgets] fix: hook 2: retry `arm`"), fix)

    def write_loop(self, reviewer_route, fixer_route):
        loop = raw_loop("widgets")
        loop["seats"]["reviewer"]["route"] = reviewer_route
        loop["seats"]["fixer"]["route"] = fixer_route
        (config.config_dir() / "widgets.json").write_text(json.dumps(loop))
        self.write_registry({reviewer_route: "reviewer", fixer_route: "fixer"})

    def test_a_seat_route_missing_from_the_registry_is_absent_not_armed(self):
        # No registry entry = no gateway binding: the hooks at the loop-file URL wake nothing.
        self.write_registry({"widgets-fix": "fixer"})
        fake = FakeGitHub(hooks(True))
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook:widgets-review ABSENT (reviewer seat) — route 'widgets-review' is "
                      "not in webhook_subscriptions.json", out)
        self.assertIn("hook 1 posts to route 'widgets-review'; route 'widgets-review' is not "
                      "in webhook_subscriptions.json", out)
        self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/1", "reader"), fake.calls)
        self.assertIn("hook 2 → paused (read back)", out)
        self.assertIn("names what each route needs (its `route:` line and fix)", out)
        # An empty registry: nothing is armed or paused, both seats named.
        self.write_registry({})
        fake = FakeGitHub(hooks(True))
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertEqual([c for c in fake.calls if c[0] == "PATCH"], [])
        self.assertIn("hook:widgets-fix ABSENT (fixer seat) — route 'widgets-fix' is not in", out)

    def test_a_route_binding_another_profile_is_the_wrong_agent_not_armed(self):
        from review_loop import doctor
        self.write_registry({"widgets-review": "someone-else", "widgets-fix": "fixer"})
        fake = FakeGitHub(hooks(True))
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook:widgets-review ABSENT (reviewer seat) — route 'widgets-review' wakes "
                      "profile 'someone-else', but seats.reviewer.profile is 'reviewer' — the "
                      "wake would run the wrong agent", out)
        self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/1", "reader"), fake.calls)
        # The review's case: the hook posts to the registry's (other-profile) URL itself. The
        # event would reach 'someone-else', not the reviewer seat — still not armed.
        drifted = hooks(True)
        drifted[1]["config"]["url"] = f"{HOST}/p/someone-else/webhooks/widgets-review"
        fake = FakeGitHub(drifted)
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("the wake would run the wrong agent", out)
        self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/1", "reader"), fake.calls)
        # doctor says the same thing about the same loop.
        from review_loop import routes
        check = doctor.check_route(config.load_id("widgets"), routes.all_routes(), "reviewer")
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("the wake would run the wrong agent", check.detail)

    def test_the_registry_url_is_the_one_credited(self):
        from review_loop import doctor
        loop = config.load_id("widgets")
        self.assertEqual(doctor.seat_hook_url(loop, "widgets-review"),
                         f"{HOST}/p/reviewer/webhooks/widgets-review")
        self.write_registry({"widgets-review": "default", "widgets-fix": "fixer"})
        loop["seats"]["reviewer"]["profile"] = "default"
        self.assertEqual(doctor.seat_hook_url(loop, "widgets-review"),
                         f"{HOST}/webhooks/widgets-review")

    def test_host_case_alone_is_the_same_url(self):
        upper = hooks(True)
        upper[1]["config"]["url"] = "HTTPS://GW.EXAMPLE/p/reviewer/webhooks/widgets-review"
        rc, out = self.arm(FakeGitHub(upper), pause=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 1 → paused (read back)", out)
        self.assertNotIn("another path", out)

    def test_a_malformed_number_is_a_clean_refusal(self):
        for key, value in (("cap", "many"), ("concurrency", "two"), ("cap", None)):
            loop = raw_loop("widgets")
            loop[key] = value
            (config.config_dir() / "widgets.json").write_text(json.dumps(loop))
            rc, out = self.arm(FakeGitHub(hooks(True)))
            self.assertEqual(rc, 2, (key, value, out))
            self.assertIn(f"{key!r} must be a whole number", out)
        loop = raw_loop("widgets")
        loop["seats"]["reviewer"]["concurrency"] = "lots"
        (config.config_dir() / "widgets.json").write_text(json.dumps(loop))
        rc, out = self.arm(FakeGitHub(hooks(True)))
        self.assertEqual(rc, 2, out)
        self.assertIn("'seats.reviewer.concurrency' must be a whole number", out)
        loop["seats"] = ["not", "an", "object"]
        (config.config_dir() / "widgets.json").write_text(json.dumps(loop))
        rc, out = self.arm(FakeGitHub(hooks(True)))
        self.assertEqual(rc, 2, out)
        self.assertIn("'seats' must be an object", out)

    def test_a_route_name_inside_another_never_credits_the_wrong_seat(self):
        # Probe (a): reviewer route "widgets" is a substring of the fixer's "widgets-fix". The
        # fixer's hook alone must not count for the reviewer too.
        self.write_loop("widgets", "widgets-fix")
        fake = FakeGitHub({2: hooks(True)[2]})
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook 2 → paused (read back)", out)
        self.assertIn("hook:widgets ABSENT (reviewer seat)", out)
        self.assertIn("pause NOT confirmed for: widgets", out)

    def test_hooks_at_a_retired_gateway_are_not_the_loops(self):
        # Probe (b): the route names are right, the origin is an old gateway's — events go
        # nowhere this loop listens, so neither seat is armed or paused by them.
        retired = {n: {**h, "config": {"url": h["config"]["url"].replace(
            HOST, "https://old-gw.example")}} for n, h in hooks(True).items() if n in (1, 2)}
        fake = FakeGitHub(retired)
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        for hook_id, route in ((1, "widgets-review"), (2, "widgets-fix")):
            self.assertIn(f"hook {hook_id} posts to route {route!r} at another origin "
                          "(https://old-gw.example, not this loop's gateway)", out)
        self.assertIn("no loop hooks found at the routes' own URLs", out)
        self.assertFalse([c for c in fake.calls if c[0] == "PATCH"], fake.calls)
        # Next to a real one: only the loop's own hook is flipped; the other seat is ABSENT.
        fake = FakeGitHub({**retired, 5: {**hooks(True)[1], "id": 5}})
        rc, out = self.arm(fake, pause=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("hook 5 → paused (read back)", out)
        self.assertIn("hook:widgets-fix ABSENT (fixer seat)", out)
        self.assertEqual([c[1] for c in fake.calls if c[0] == "PATCH"],
                         ["/repos/owner/widgets/hooks/5"])

    def test_another_profile_or_path_on_the_loops_gateway_is_not_that_seats_hook(self):
        # The gateway binds a route to its profile by URL and answers another profile's URL 404,
        # so such a hook wakes nothing: never flipped, and the seat is ABSENT (rc 1).
        for url, cause in ((f"{HOST}/p/someone-else/webhooks/widgets-review",
                            "another profile ('someone-else'; the route is bound to 'reviewer'"),
                           (f"{HOST}/webhooks/widgets-review", "another profile ('default'"),
                           (f"{HOST}/hooks/x/webhooks/widgets-review", "another path")):
            moved = hooks(True)
            moved[1]["config"]["url"] = url
            fake = FakeGitHub(moved)
            rc, out = self.arm(fake, pause=True)
            self.assertEqual(rc, 1, (url, out))
            self.assertIn(f"hook 1 posts to route 'widgets-review' at {cause}", out)
            self.assertIn("hook:widgets-review ABSENT (reviewer seat)", out)
            self.assertNotIn(("PATCH", "/repos/owner/widgets/hooks/1", "reader"), fake.calls)
            self.assertIn("hook 2 → paused (read back)", out)

    def test_doctor_names_the_actual_cause_of_a_mismatch(self):
        from review_loop import doctor
        loop = config.load_id("widgets")
        url = f"{HOST}/p/reviewer/webhooks/widgets-review"
        for posted, cause in ((f"{HOST}/p/someone-else/webhooks/widgets-review", "another profile"),
                              ("https://old-gw.example/p/reviewer/webhooks/widgets-review",
                               "another origin"),
                              (f"{HOST}/hooks/x/webhooks/widgets-review", "another path")):
            hook = {"id": 1, "active": True, "events": ["pull_request"],
                    "config": {"url": posted, "content_type": "json"}}
            check = doctor.check_hook(loop, [hook], "reviewer", "widgets-review", url)
            self.assertEqual(check.status, doctor.MISMATCH, posted)
            self.assertIn(f"posts to {cause}", check.detail)

    def test_a_successful_run_prints_no_fix_line(self):
        rc, out = self.arm(FakeGitHub(hooks(True)), pause=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual([line for line in out.splitlines() if "fix:" in line], [])

    def test_two_seats_on_one_route_are_refused_on_load(self):
        self.write_loop("widgets-review", "widgets-review")
        with self.assertRaisesRegex(config.ConfigError, r"seats.reviewer.route and "
                                    r"seats.fixer.route are both 'widgets-review'"):
            config.load_id("widgets")
        rc, out = self.arm(FakeGitHub({1: hooks(True)[1]}), pause=True)
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot pause:", out)
        self.assertIn("each seat needs its own route", out)

    def test_fix_lines_are_formatted_alike(self):
        outs = [self.arm(FakeGitHub(hooks(True), list_error="HTTP 404"))[1],
                self.arm(FakeGitHub(hooks(True), patch_error="HTTP 403"), pause=True)[1],
                self.arm(FakeGitHub({1: hooks(False)[1]}))[1]]
        for out in outs:
            fix = [line for line in out.splitlines() if "fix:" in line]
            self.assertTrue(fix, out)
            for line in fix:
                self.assertTrue(line.startswith("[widgets] fix: "), repr(line))

    def test_no_loops_or_unknown_loop_fail(self):
        (config.config_dir() / "widgets.json").unlink()
        rc, out = self.arm(FakeGitHub({}), loop=None)
        self.assertEqual(rc, 2, out)
        rc, out = self.arm(FakeGitHub({}), loop="nope")
        self.assertEqual(rc, 2, out)


class ScheduleFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes")})
        env.start()
        self.addCleanup(env.stop)
        self.hermes = self.root / "bin dir" / "hermes"
        self.hermes.parent.mkdir()

    def fake_hermes(self, rc):
        self.hermes.write_text(f"#!/bin/sh\necho 'cron: store is locked' >&2\nexit {rc}\n")
        self.hermes.chmod(0o755)

    def test_failed_cron_create_is_not_ok_and_fallback_is_quoted(self):
        self.fake_hermes(1)
        with patch.object(cli.shutil, "which", return_value=str(self.hermes)):
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertFalse(ok)
        self.assertIn("store is locked", lines[0])
        command = lines[1].split("run it yourself: ", 1)[1]
        self.assertEqual(shlex.split(command)[:6],
                         [str(self.hermes), "cron", "create", "15m", "--name",
                          "review loop watchdog (widgets)"])
        subprocess.run(["bash", "-n", "-c", command], check=True)

    def test_init_exits_nonzero_when_the_job_was_not_created(self):
        self.fake_hermes(1)
        hermes_home = self.root / "hermes"
        for profile in ("rp", "fp"):
            (hermes_home / "profiles" / profile).mkdir(parents=True)
            (hermes_home / "profiles" / profile / "config.yaml").write_text("model: x\n")
        tokens = self.root / "tokens"
        tokens.mkdir()
        for login in ("rv", "fx", "rd"):
            (tokens / login).write_text("dummy")
            (tokens / login).chmod(0o600)

        class Ctx:
            def register_cli_command(self, name, help_text, setup, **kw):
                self.setup = setup

        ctx = Ctx()
        cli.register_cli(ctx, {})
        parser = argparse.ArgumentParser()
        ctx.setup(parser)
        args = parser.parse_args([
            "init", "--repo", "acme/gadgets", "--fixer", "fx", "--reviewer", "rv",
            "--host", HOST, "--reviewer-profile", "rp", "--fixer-profile", "fp",
            "--token", f"rv={tokens / 'rv'}", "--token", f"fx={tokens / 'fx'}",
            "--read-token", "rd", "--token", f"rd={tokens / 'rd'}",   # the reader is its own account
            "--schedule", "15m"])
        out = io.StringIO()
        with patch.dict(os.environ, {"REVIEW_LOOP_CONFIG_DIR": str(self.root / "configs")}), \
                patch.object(cli.shutil, "which", return_value=str(self.hermes)), \
                redirect_stdout(out):
            rc = args.func(args)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("init INCOMPLETE", out.getvalue())
        self.assertNotIn("Next:", out.getvalue())

    def test_successful_cron_create_is_ok(self):
        self.fake_hermes(0)
        with patch.object(cli.shutil, "which", return_value=str(self.hermes)):
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertTrue(ok, lines)


if __name__ == "__main__":
    unittest.main()
