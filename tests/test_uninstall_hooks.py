#!/usr/bin/env python3
"""Issue #57 (and #83's tracebacks): uninstall leaves nothing live, init never doubles hooks.

GitHub is the harness's stateful stub (hook POST/PATCH/DELETE change its world file) and the
scheduler is the harness's fake ``hermes`` — no test here can reach a real repo or install.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import pathlib
import shlex
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import cli, config, doctor, routes  # noqa: E402

LOOP_FILE = t.LOOPS_DIR / "widgets.json"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        self.world(hooks=[])
        self.hermes_log = t.TMP / "hermes-calls.jsonl"
        self.hermes_log.unlink(missing_ok=True)
        os.environ["FAKE_HERMES_LOG"] = str(self.hermes_log)
        os.environ["REVIEW_LOOP_HERMES"] = str(t.FAKE_HERMES)
        self.addCleanup(os.environ.pop, "FAKE_HERMES_LOG", None)

    # -- helpers -------------------------------------------------------------------

    def world(self, **changes) -> dict:
        data = json.loads(t.WORLD_FILE.read_text())
        data.update(changes)
        t.WORLD_FILE.write_text(json.dumps(data))
        return data

    def hooks(self) -> list[dict]:
        return json.loads(t.WORLD_FILE.read_text()).get("hooks") or []

    def cli(self, *argv) -> tuple[int, str]:
        return t.run_cli(t.parser_for().parse_args(list(argv)))

    def init(self, *extra) -> tuple[int, str]:
        return self.cli("init", "--repo", t.REPO, "--id", "widgets", "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra)

    def fresh_install(self) -> None:
        """A clean `init --hooks` of the widgets loop (the fixture's hand-written one removed)."""
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 2)

    def cron_store(self, jobs: list[dict]) -> pathlib.Path:
        store = doctor.cron_store()
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps({"jobs": jobs}))
        return store

    def job(self, loop_id: str, job_id: str) -> dict:
        return {"id": job_id, "name": f"review loop watchdog ({loop_id})",
                "script": cli.SHIM_NAME, "no_agent": True}


class RoundTripTest(Base):
    def test_uninstall_then_init_leaves_exactly_one_set_of_hooks(self):
        self.fresh_install()
        first = {hook["id"] for hook in self.hooks()}
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [])
        for hook_id in first:
            self.assertIn(f"hook {hook_id} deleted", out)
        self.assertFalse(LOOP_FILE.exists())
        self.assertIsNone(routes.route("widgets-review"))
        self.assertNotIn(" arm ", out)  # no remediation that needs the config it just removed
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        hooks = self.hooks()
        self.assertEqual(len(hooks), 2)
        self.assertFalse(first & {hook["id"] for hook in hooks})
        self.assertTrue(all(hook["active"] is False for hook in hooks))

    def test_init_refuses_hooks_left_by_a_previous_install(self):
        self.fresh_install()
        stale = sorted(hook["id"] for hook in self.hooks())
        self.world(hooks=[{**hook, "active": True} for hook in self.hooks()])
        # The old uninstall: routes and config gone, hooks left live.
        rc, out = self.cli("uninstall", "--loop", "widgets", "--keep-hooks")
        self.assertEqual(rc, 0, out)
        subs_before = t.SUBS.read_text()
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn(", ".join(str(i) for i in stale), out)
        self.assertIn("2 active", out)
        for hook_id in stale:
            command = f"gh api -X DELETE repos/{t.REPO}/hooks/{hook_id}"
            self.assertIn(command, out)
            self.assertEqual(shlex.split(command)[-1], f"repos/{t.REPO}/hooks/{hook_id}")
        self.assertFalse(LOOP_FILE.exists())
        self.assertEqual(t.SUBS.read_text(), subs_before)
        self.assertEqual(sorted(hook["id"] for hook in self.hooks()), stale)

    def test_init_refuses_when_the_hook_listing_cannot_be_read(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.world(hooks=None)
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_init_ignores_other_routes_hooks(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        other = {"id": 7, "active": True, "events": ["push"],
                 "config": {"url": f"{t.HOST}/webhooks/widgets-review-impostor"}}
        self.world(hooks=[other])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 3)

    def test_hooks_on_another_gateway_are_info_not_a_refusal(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        foreign = {"id": 8, "active": True, "events": ["pull_request"],
                   "config": {"url": "https://old-gateway.example/p/reviewer-profile/webhooks/widgets-review"}}
        self.world(hooks=[foreign])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 8", out)
        self.assertIn("another origin (https://old-gateway.example", out)
        self.assertEqual(len(self.hooks()), 3)

    def test_a_hook_at_the_routes_own_url_refuses(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        stale = {"id": 8, "active": True, "events": ["pull_request"],
                 "config": {"url": f"{t.HOST}/p/reviewer-profile/webhooks/widgets-review"}}
        self.world(hooks=[stale])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 2, out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/8", out)

    def test_another_profile_on_the_same_gateway_is_info_not_a_collision(self):
        # The gateway answers /p/<other profile>/webhooks/<route> 404: that hook never reaches
        # these routes, so it is reported (with its cause) and never deleted or refused over.
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        other = {"id": 8, "active": True, "events": ["pull_request"],
                 "config": {"url": f"{t.HOST}/p/old-profile/webhooks/widgets-review"}}
        self.world(hooks=[other])
        rc, out = self.init("--hooks")
        self.assertEqual(rc, 0, out)
        self.assertIn("info: hook 8 posts to route 'widgets-review' at another profile "
                      "('old-profile'; the route is bound to 'reviewer-profile'", out)
        self.assertEqual(len(self.hooks()), 3)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [other])       # uninstall deletes only its own URLs
        self.assertIn("hook 8 posts to route 'widgets-review' at another profile", out)


class UninstallRefusalTest(Base):
    def test_a_token_github_refuses_leaves_everything_and_prints_pasteable_commands(self):
        self.fresh_install()
        ids = sorted(hook["id"] for hook in self.hooks())
        self.world(hook_write_denied=[t.READ_LOGIN])
        subs_before = t.SUBS.read_text()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("HTTP 403", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertEqual(t.SUBS.read_text(), subs_before)
        self.assertEqual(sorted(hook["id"] for hook in self.hooks()), ids)
        for hook_id in ids:
            self.assertIn(f"  gh api -X DELETE repos/{t.REPO}/hooks/{hook_id}\n", out)
        self.assertIn("hermes review-loop uninstall --loop widgets --admin-token <login>", out)
        # The printed re-run still parses: the config it needs is still there.
        t.parser_for().parse_args(["uninstall", "--loop", "widgets"])
        rc, out = self.cli("uninstall", "--loop", "widgets", "--admin-token", t.FIXER)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [])
        self.assertFalse(LOOP_FILE.exists())

    def test_unmapped_admin_token_is_refused_before_anything(self):
        self.fresh_install()
        rc, out = self.cli("uninstall", "--loop", "widgets", "--admin-token", "stranger")
        self.assertEqual(rc, 2, out)
        self.assertIn("no token file mapped", out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertTrue(LOOP_FILE.exists())

    def test_unreadable_listing_refuses_with_a_find_command(self):
        self.world(hooks=None)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)

    def test_keep_hooks_is_an_explicit_opt_out(self):
        self.fresh_install()
        rc, out = self.cli("uninstall", "--loop", "widgets", "--keep-hooks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertIn("kept (--keep-hooks)", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_hooks_on_another_gateway_are_not_deleted(self):
        foreign = {"id": 9, "active": True, "events": ["pull_request"],
                   "config": {"url": "https://other.example/webhooks/widgets-review"}}
        self.world(hooks=[foreign])
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hooks(), [foreign])
        self.assertIn("hook 9", out)
        self.assertIn("another origin (https://other.example", out)


    def test_the_honest_branch_is_keyed_on_structure_not_prose(self):
        # #131(1): _uninstall_refused chose its remedy by pattern-matching the reason's *wording*
        # (``reason.startswith("could not confirm the deletion of hook")``). Rewording the emitter
        # that produced the reason silently dropped the honest branch -- "every DELETE was accepted
        # but could not be read back" -- and fell through to a different remedy, even though every
        # DELETE succeeded and no hook is left. The honest case is a structured fact (accepted-but-
        # unread), so the dispatcher must take it as one, never by string prefix.
        loop = {"id": "widgets", "repo": t.REPO}
        args = argparse.Namespace(keep_config=False)
        # The same fact, two spellings of the reason: only the structure may decide.
        for wording in ("could not confirm the deletion of hooks 5 (read-back failed)",
                        "could not verify the deletion of hooks 5 (read-back failed)"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli._uninstall_refused(loop, [wording], [], args, accepted_unread=True)
            out = buf.getvalue()
            self.assertIn("every DELETE was accepted", out, wording)
            self.assertNotIn("still live", out, wording)
            self.assertNotIn("could not be read or confirmed", out, wording)


class HostlessTest(Base):
    def blank_host(self) -> None:
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["host"] = ""
        LOOP_FILE.write_text(json.dumps(cfg))

    def test_a_host_less_loop_with_no_hook_on_its_routes_uninstalls(self):
        other = {"id": 9, "active": True, "events": ["pull_request"],
                 "config": {"url": "https://elsewhere.example/webhooks/ci"}}
        self.world(hooks=[other])
        self.blank_host()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("still live", out)
        self.assertIn("hooks: none — the loop has no host, and no repo hook posts to its route "
                      "names", out)
        self.assertEqual(self.hooks(), [other])  # nothing on GitHub was touched
        self.assertFalse(LOOP_FILE.exists())
        self.assertIsNone(routes.route("widgets-review"))

    def test_a_blanked_host_never_leaves_its_own_hooks_live(self):
        # The review's repro: install with hooks, arm, blank the host, uninstall. The hooks may be
        # this install's; skipping them would leave them live with routes and config gone.
        self.fresh_install()
        self.world(hooks=[{**hook, "active": True} for hook in self.hooks()])
        ids = sorted(hook["id"] for hook in self.hooks())
        self.blank_host()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"hooks {ids[0]}, {ids[1]} post to this loop's route names", out)
        # Nothing ties them to this install (no host, so no route URL to compare): the
        # remediation is to look, never a DELETE that might hit another install's live hook.
        self.assertNotIn("gh api -X DELETE", out)
        self.assertNotIn("still live. Delete them", out)
        self.assertIn("another install's", out)
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)
        self.assertIn("--host https://your-gateway.example", out)
        self.assertEqual(sorted(hook["id"] for hook in self.hooks()), ids)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))
        self.assertNotIn("route removed", out)

    def test_a_host_less_listing_with_an_unidentifiable_entry_refuses(self):
        # _classify_hooks' rule: a matching entry without an integer id makes the listing
        # untrustworthy, never "no hooks" (which would remove routes and config over live hooks).
        self.world(hooks=[{"id": "7", "active": True, "events": ["pull_request"],
                           "config": {"url": "https://gw.example/p/x/webhooks/widgets-review"}}])
        self.blank_host()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("invalid hook listing", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))

    def test_an_unreadable_listing_refuses_a_host_less_uninstall(self):
        self.world(hooks=None)
        self.blank_host()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("could not read the repo's hooks", out)
        self.assertTrue(LOOP_FILE.exists())

    def test_set_refuses_a_blank_or_invalid_host(self):
        before = LOOP_FILE.read_bytes()
        for value, reason in (("   ", "a blank host would orphan the loop's hooks"),
                              ("", "a blank host would orphan the loop's hooks"),
                              ("gateway.local", "")):
            rc, out = self.cli("set", "--loop", "widgets", "--host", value)
            self.assertEqual(rc, 2, (value, out))
            self.assertIn("refused:", out)
            self.assertIn(reason, out)
            self.assertEqual(LOOP_FILE.read_bytes(), before)


class CronTest(Base):
    def test_uninstall_removes_its_job_through_the_fake_scheduler_only(self):
        self.cron_store([self.job("widgets", "job1"), self.job("gadgets", "job2")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        calls = [json.loads(line) for line in self.hermes_log.read_text().splitlines()]
        self.assertEqual(calls, [["cron", "remove", "job1"]])
        jobs = json.loads(doctor.cron_store().read_text())["jobs"]
        self.assertEqual([job["id"] for job in jobs], ["job2"])
        self.assertTrue(shim.exists(), "another loop's job still runs the shared shim")

    def test_the_shared_shim_goes_with_the_last_job(self):
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertFalse(shim.exists())

    def test_a_job_that_will_not_go_refuses_before_routes_and_config(self):
        self.cron_store([self.job("widgets", "job1")])
        os.environ["REVIEW_LOOP_HERMES"] = "/bin/false"
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("hermes cron remove job1", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))

    def test_an_unreadable_store_refuses_before_any_hook_is_deleted(self):
        self.fresh_install()
        doctor.cron_store().write_text("{broken")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.hooks()), 2)


class PurgeTest(Base):
    def default_state(self) -> pathlib.Path:
        target = config.home() / "state" / "review-loops" / "widgets"
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["state_dir"] = str(target)
        LOOP_FILE.write_text(json.dumps(cfg))
        (target / "sub").mkdir(parents=True, exist_ok=True)
        (target / "sub" / "x.json").write_text("{}")
        return target

    def test_purge_removes_the_default_state_dir_only(self):
        target = self.default_state()
        neighbour = target.parent / "gadgets"
        neighbour.mkdir()
        outside = t.TMP / "outside.txt"
        outside.write_text("keep")
        (target / "link").symlink_to(outside)
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(target.exists())
        self.assertTrue(neighbour.exists())
        self.assertEqual(outside.read_text(), "keep")

    def test_purge_refuses_a_custom_state_dir(self):
        t.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (t.STATE_DIR / "keep.json").write_text("{}")
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn("not the default", out)
        self.assertIn(f"rm -rf -- {shlex.quote(str(t.STATE_DIR))}", out)
        self.assertIn("hermes review-loop uninstall --loop widgets &&", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertTrue((t.STATE_DIR / "keep.json").exists())

    def test_a_custom_state_dir_with_spaces_is_quoted(self):
        odd = t.TMP / "my state's dir"
        odd.mkdir()
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["state_dir"] = str(odd)
        LOOP_FILE.write_text(json.dumps(cfg))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        line = next(l for l in out.splitlines() if "rm -rf --" in l)
        self.assertEqual(shlex.split(line.split("&&")[-1])[-1], str(odd))
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"rm -rf -- {shlex.quote(str(odd))}", out)
        self.assertTrue(odd.exists())

    def test_purge_refuses_a_symlinked_state_dir(self):
        target = self.default_state()
        real = t.TMP / "real-state"
        target.rename(real)
        target.symlink_to(real)
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn("symlink", out)
        self.assertIn(f"rm -- {shlex.quote(str(target))}", out)
        self.assertTrue((real / "sub" / "x.json").exists())
        self.assertTrue(LOOP_FILE.exists())
        # It cannot know where the link points — here, the loop's own moved state — so it says
        # so and tells the operator to look, like the mid-run symlink branch does.
        self.assertNotIn("the target is outside", out)
        self.assertIn("it may point at this loop's own (moved) state", out)
        self.assertIn(f"ls -ld -- {shlex.quote(str(target))}   # where it points", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_state_dir_that_will_not_go_is_reported_not_raised(self):
        # Hooks, cron, routes and config are already gone when the recursive delete runs: a
        # PermissionError there must end in a summary and the exact command, never a traceback.
        self.fresh_install()
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        target = self.default_state()
        locked = target / "sub"
        locked.chmod(0o500)  # x.json cannot be unlinked from a read-only parent
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.hooks(), [])
        self.assertFalse(LOOP_FILE.exists())
        self.assertIsNone(routes.route("widgets-review"))
        self.assertTrue((locked / "x.json").exists())
        self.assertNotIn("state removed", out)
        self.assertIn(f"state NOT removed: {target}", out)
        self.assertIn(f"Permission denied: {locked / 'x.json'}", out)  # the full path, not 'x.json'
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        for part in ("repo hooks", "watchdog job", "cron shim", "routes", "config"):
            self.assertIn(part, summary)
        self.assertIn("left behind", summary)
        command = f"rm -rf -- {shlex.quote(str(target))}"
        self.assertIn(f"  {command}\n", out)
        locked.chmod(0o700)
        os.system(command)  # the printed command is the one that finishes the job
        self.assertFalse(target.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_an_unreadable_state_dir_refuses_the_purge_before_anything(self):
        # The in-flight check takes the state lock inside the directory about to be deleted.
        self.fresh_install()
        target = self.default_state()
        target.chmod(0o500)                     # state.lock cannot be created
        self.addCleanup(lambda: target.exists() and target.chmod(0o700))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn("refused: --purge cannot check for a run in flight", out)
        self.assertIn(f"rm -rf -- {shlex.quote(str(target))}", out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_config_that_will_not_go_is_reported_not_raised(self):
        self.fresh_install()
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        target = self.default_state()
        LOOP_FILE.parent.chmod(0o500)            # the config file cannot be unlinked
        self.addCleanup(LOOP_FILE.parent.chmod, 0o700)
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.hooks(), [])
        self.assertIsNone(routes.route("widgets-review"))
        self.assertTrue(LOOP_FILE.exists())
        self.assertTrue(target.exists())
        self.assertIn(f"config NOT removed: {LOOP_FILE}", out)
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        for part in ("repo hooks", "watchdog job", "cron shim", "routes"):
            self.assertIn(part, summary.split("; left behind")[0])
        self.assertIn(f"left behind: the loop config {LOOP_FILE}", summary)
        self.assertIn("the state directory", summary)
        self.assertIn(f"  rm -f -- {shlex.quote(str(LOOP_FILE))}\n", out)
        self.assertIn(f"  rm -rf -- {shlex.quote(str(target))}\n", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_cron_shim_that_will_not_go_makes_the_run_incomplete(self):
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        shim.parent.chmod(0o500)                 # the shim cannot be unlinked
        self.addCleanup(shim.parent.chmod, 0o700)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"cron shim NOT removed: {shim}", out)
        self.assertFalse(LOOP_FILE.exists())     # everything else still went
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        self.assertIn(f"left behind: the cron shim {shim}", summary)
        self.assertNotIn("cron shim,", summary.split("; left behind")[0])
        self.assertIn(f"  rm -- {shlex.quote(str(shim))}\n", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_shim_and_a_state_dir_that_will_not_go_are_both_left_behind(self):
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        target = self.default_state()
        locked = target / "sub"
        shim.parent.chmod(0o500)
        self.addCleanup(shim.parent.chmod, 0o700)
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        left = summary.split("; left behind: ", 1)[1]
        self.assertIn(f"the cron shim {shim}", left)
        self.assertIn(f"the state directory {target}", left)
        self.assertIn(f"  rm -- {shlex.quote(str(shim))}\n", out)
        self.assertIn(f"  rm -rf -- {shlex.quote(str(target))}\n", out)

    def test_an_unparseable_route_registry_refuses_before_anything_is_removed(self):
        # The review's repro: the registry is rewritten by step 3, after hooks and the job are
        # gone. It is read first now, so nothing is touched and there is no traceback.
        self.fresh_install()
        self.cron_store([self.job("widgets", "job1")])
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("#!/bin/sh\n")
        t.SUBS.write_text("{broken")
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"route registry {t.SUBS} cannot be read", out)
        self.assertIn("nothing below the failure was touched", out)
        self.assertEqual(len(self.hooks()), 2)
        self.assertTrue(self.hermes_log.exists() is False or
                        "remove" not in self.hermes_log.read_text())
        self.assertTrue(shim.exists())
        self.assertTrue(LOOP_FILE.exists())

    def test_a_route_step_that_fails_late_is_incomplete_not_a_traceback(self):
        self.fresh_install()
        real = routes.remove_route

        def refuse(name):
            raise routes.RegistryConflictError("route registry kept changing under the edit")
        routes.remove_route = refuse
        self.addCleanup(setattr, routes, "remove_route", real)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.hooks(), [])
        self.assertIn("routes NOT removed: route registry kept changing", out)
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        self.assertIn("removed: repo hooks", summary)
        self.assertIn("left behind: the routes widgets-review, widgets-fix", summary)
        self.assertIn("  hermes review-loop uninstall --loop widgets   # once", out)
        self.assertTrue(LOOP_FILE.exists())

    def test_a_symlinked_shim_is_named_as_left_behind(self):
        self.cron_store([])
        target = t.TMP / "real-shim.py"
        target.write_text("#!/bin/sh\n")
        shim = config.home() / "scripts" / cli.SHIM_NAME
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.symlink_to(target)
        self.addCleanup(lambda: shim.is_symlink() and shim.unlink())
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"cron shim NOT removed: {shim} is a symlink", out)
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        self.assertIn(f"left behind: the cron shim {shim}", summary)
        self.assertTrue(shim.is_symlink())
        self.assertTrue(target.exists())

    def test_a_config_the_loader_refuses_gets_the_lookup_advice(self):
        self.fresh_install()
        self.cron_store([self.job("widgets", "job1")])
        cfg = json.loads(LOOP_FILE.read_text())
        cfg["seats"]["fixer"]["route"] = cfg["seats"]["reviewer"]["route"]   # both seats, one route
        LOOP_FILE.write_text(json.dumps(cfg))
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn("each seat needs its own route", out)
        self.assertIn("it may still have live repo hooks and a watchdog job; nothing was touched", out)
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)
        self.assertIn("hermes cron remove job1", out)
        self.assertEqual(len(self.hooks()), 2)

    def test_a_state_dir_swapped_for_a_symlink_mid_run_is_incomplete(self):
        # The review's repro: the directory is swapped for a link after _purge_target vetted it
        # (inside the in-flight check). By then hooks, job, routes and config are going; the
        # run must end INCOMPLETE with its summary, not a bare "refused" and rc 2.
        self.fresh_install()
        self.cron_store([self.job("widgets", "job1")])
        target = self.default_state()
        real = t.TMP / "moved-state"

        def swap(loop, roles):
            target.rename(real)
            target.symlink_to(real)
            return []
        original = cli._busy_seats
        cli._busy_seats = swap
        self.addCleanup(setattr, cli, "_busy_seats", original)
        self.addCleanup(lambda: target.is_symlink() and target.unlink())
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"state NOT removed: {target} became a symlink after it was checked", out)
        summary = next(line for line in out.splitlines() if line.startswith("uninstall INCOMPLETE"))
        self.assertIn("removed: repo hooks, watchdog job", summary)
        self.assertIn(f"the state directory {target} (now a symlink", summary)
        self.assertIn(f"  rm -- {shlex.quote(str(target))}   # the link itself", out)
        self.assertTrue((real / "sub" / "x.json").exists())      # never followed
        self.assertFalse(LOOP_FILE.exists())

    def test_deletes_accepted_but_not_read_back_are_not_called_live(self):
        self.fresh_install()
        ids = sorted(hook["id"] for hook in self.hooks())
        original = cli._loop_hooks
        cli._loop_hooks = lambda loop, login: (None, "HTTP 502 Bad Gateway")
        self.addCleanup(setattr, cli, "_loop_hooks", original)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.hooks(), [])                       # they really are gone
        self.assertIn(f"could not confirm the deletion of hooks {ids[0]}, {ids[1]} (GitHub "
                      "accepted each DELETE", out)
        self.assertIn("every DELETE was accepted, but the hook listing could not be read back", out)
        self.assertNotIn("still live", out)
        self.assertNotIn("gh api -X DELETE", out)
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)
        self.assertTrue(LOOP_FILE.exists())

    def test_no_hooks_of_ours_and_a_failed_read_back_is_unconfirmed_not_live(self):
        # The first listing found none of this loop's hooks. The second sample is still taken
        # (it is what catches a hook created meanwhile); when it fails, the refusal says the
        # absence could not be confirmed — no "still live", no DELETE template.
        self.world(hooks=[])
        calls = []
        original = cli._loop_hooks

        def failing(loop, login):
            calls.append(login)
            return None, "HTTP 502 Bad Gateway"
        cli._loop_hooks = failing
        self.addCleanup(setattr, cli, "_loop_hooks", original)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(calls and len(calls), 1, "the read-back must be taken")
        self.assertEqual(rc, 2, out)
        self.assertIn("could not confirm that no hook of this loop's appeared while uninstall ran",
                      out)
        self.assertNotIn("still live", out)
        self.assertNotIn("gh api -X DELETE", out)
        self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)
        self.assertTrue(LOOP_FILE.exists())

    def test_a_hook_that_appears_during_uninstall_is_caught(self):
        # The review's race: none of ours on the first listing, one on the second (a concurrent
        # arm or init --hooks). Refused and named — never "decommissioned" with a live hook.
        self.fresh_install()
        live = self.hooks()
        self.world(hooks=[])
        original = cli._loop_hooks
        cli._loop_hooks = lambda loop, login: (live, "")
        self.addCleanup(setattr, cli, "_loop_hooks", original)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        ids = sorted(hook["id"] for hook in live)
        self.assertIn(f"hooks {ids[0]}, {ids[1]} appeared on this loop's route URLs while "
                      "uninstall ran", out)
        self.assertTrue(LOOP_FILE.exists())
        self.assertIsNotNone(routes.route("widgets-review"))

    def test_an_unreadable_listing_asserts_nothing_and_hands_out_no_delete(self):
        # Hosted and host-less alike: nothing was classified, so no liveness claim and no
        # destructive template — the find command, and look at each URL first.
        for blank in (False, True):
            if blank:
                cfg = json.loads(LOOP_FILE.read_text())
                cfg["host"] = ""
                LOOP_FILE.write_text(json.dumps(cfg))
            self.world(hooks=None)
            # The test's own name: nothing was classified, so no read-back is taken either. Without
            # this the suite would pass if the code took a read-back and simply ignored its error
            # (#131).
            readbacks: list = []
            original = cli._loop_hooks

            def spy(loop, login):
                readbacks.append(login)
                return original(loop, login)
            cli._loop_hooks = spy
            self.addCleanup(setattr, cli, "_loop_hooks", original)
            rc, out = self.cli("uninstall", "--loop", "widgets")
            self.assertEqual(rc, 2, (blank, out))
            self.assertEqual(readbacks, [], "no read-back may be attempted when nothing was read")
            self.assertNotIn("still live", out)
            self.assertNotIn("gh api -X DELETE", out)
            self.assertIn("check each one's URL before deleting anything", out)
            self.assertIn(f"gh api 'repos/{t.REPO}/hooks?per_page=100' --jq", out)

    def test_a_failed_delete_is_live_and_an_accepted_one_unconfirmed(self):
        # Recorded as they fail, not recovered from the failure prose: hook A's DELETE fails and
        # is named live with its DELETE command; hook B's was accepted and is only unconfirmed.
        self.fresh_install()
        first, second = sorted(hook["id"] for hook in self.hooks())
        real_fetch = cli.gh.fetch

        def fetch(loop, path, method="GET", body=None, login=None):
            if method == "DELETE" and path.endswith(f"/hooks/{first}"):
                return None, "HTTP 500 Internal Server Error"
            return real_fetch(loop, path, method=method, body=body, login=login)
        original = cli._loop_hooks
        cli.gh.fetch = fetch
        cli._loop_hooks = lambda loop, login: (None, "HTTP 502 Bad Gateway")
        self.addCleanup(setattr, cli.gh, "fetch", real_fetch)
        self.addCleanup(setattr, cli, "_loop_hooks", original)
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"hook {first}: DELETE failed (HTTP 500", out)
        self.assertIn(f"could not confirm the deletion of hook {second} (GitHub accepted", out)
        self.assertIn("still live", out)
        self.assertIn(f"  gh api -X DELETE repos/{t.REPO}/hooks/{first}\n", out)
        self.assertNotIn(f"hooks/{second}\n", out)

    def test_purging_the_last_loop_forgets_the_ledger_presence(self):
        # #113's contract: when the last loop is uninstalled, forget that a run ledger existed
        # (run_supervisor.forget_ledger_presence), so a later fresh install is not reported as a
        # vanished ledger. Through #57's full --purge path: state dir gone first, then the marker.
        from review_loop import run_supervisor
        self.fresh_install()
        target = self.default_state()
        marker = run_supervisor.presence_marker()
        marker.write_text("ledger")
        other = t.LOOPS_DIR / "other.json"
        other.write_text(LOOP_FILE.read_text().replace('"widgets"', '"other"'))
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(target.exists())
        self.assertTrue(marker.exists(), "another loop is still configured")
        other.unlink()
        self.init("--hooks")
        target = self.default_state()
        rc, out = self.cli("uninstall", "--loop", "widgets", "--purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(target.exists())
        self.assertFalse(marker.exists())
        self.assertIn(f"ledger presence marker removed: {marker}", out)
        self.assertLess(out.index("state removed:"), out.index("ledger presence marker removed"))

    def test_without_purge_default_state_is_kept_and_named(self):
        self.default_state()
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertIn("pass --purge", out)


class DoctorDuplicateTest(Base):
    def test_two_hooks_on_one_route_fail_even_when_one_is_active(self):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hooks = [{"id": 3, "active": True, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}},
                 {"id": 101, "active": False, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}}]
        check = doctor.check_hook(loop, hooks, "reviewer", "widgets-review", url)
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("2 repo hooks", check.detail)
        self.assertIn("1 active", check.detail)
        self.assertIn("`gh api -X DELETE repos/acme/widgets/hooks/3`", check.fix)
        self.assertIn("uninstall --loop widgets", check.fix)
        self.assertNotIn("hooks/101", check.fix)  # the newest is the one kept
        single = doctor.check_hook(loop, hooks[:1], "reviewer", "widgets-review", url)
        self.assertEqual(single.status, doctor.VERIFIED)

    def test_two_hooks_on_different_gateways_are_not_duplicates(self):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hooks = [{"id": 3, "active": True, "events": ["pull_request"],
                  "config": {"url": "https://old.example/webhooks/widgets-review",
                             "content_type": "json"}},
                 {"id": 101, "active": True, "events": ["pull_request"],
                  "config": {"url": url, "content_type": "json"}}]
        check = doctor.check_hook(loop, hooks, "reviewer", "widgets-review", url)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)


class DoctorDeliveriesTest(Base):
    """The one place GitHub reveals a secret mismatch: how the gateway answered its deliveries."""

    def check(self, deliveries):
        loop = config.load_id("widgets")
        url = routes.url_for("widgets-review", t.HOST)
        hook = {"id": 5, "active": True, "events": ["pull_request"],
                "config": {"url": url, "content_type": "json"}}
        self.world(hooks=[hook], deliveries={"5": deliveries})
        return doctor.check_hook(loop, [hook], "reviewer", "widgets-review", url)

    @staticmethod
    def delivery(n, code, at):
        return {"id": n, "status_code": code, "delivered_at": at, "event": "pull_request",
                "status": "OK" if code < 300 else f"Invalid HTTP Response: {code}"}

    def test_latest_delivery_rejected_401_is_a_secret_mismatch(self):
        check = self.check([self.delivery(2, 401, "2026-09-02T00:00:00Z"),
                            self.delivery(1, 202, "2026-09-01T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("401", check.detail)
        self.assertIn("secret", check.detail)
        self.assertIn("uninstall --loop widgets", check.fix)
        self.assertIn("init", check.fix)

    def test_order_is_read_from_delivered_at_not_list_position(self):
        check = self.check([self.delivery(1, 202, "2026-09-01T00:00:00Z"),
                            self.delivery(2, 401, "2026-09-02T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)

    def test_a_403_is_a_refused_route(self):
        check = self.check([self.delivery(2, 403, "2026-09-02T00:00:00Z")])
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("403", check.detail)

    def test_a_later_success_clears_an_old_rejection(self):
        check = self.check([self.delivery(2, 202, "2026-09-02T00:00:00Z"),
                            self.delivery(1, 401, "2026-09-01T00:00:00Z")])
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("latest delivery 202", check.detail)

    def test_no_deliveries_yet_says_the_secret_is_unproven(self):
        check = self.check([])
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("no deliveries yet", check.detail)

    def test_a_delivery_with_no_response_is_unproven_not_green(self):
        check = self.check([{"id": 2, "status_code": None, "delivered_at": "2026-09-02T00:00:00Z",
                             "event": "pull_request", "status": "timed out"},
                            self.delivery(1, 202, "2026-09-01T00:00:00Z")])
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("no HTTP response", check.detail)
        self.assertIn("timed out", check.detail)
        self.assertIn("--ping", check.fix)
        check = self.check([{"id": 3, "status_code": 0, "delivered_at": "2026-09-03T00:00:00Z",
                             "event": "pull_request", "status": "connection refused"}])
        self.assertEqual(check.status, doctor.UNKNOWN)

    def test_a_5xx_delivery_is_failing_not_green(self):
        for code in (500, 502, 404):
            check = self.check([self.delivery(2, code, "2026-09-02T00:00:00Z")])
            self.assertEqual(check.status, doctor.MISMATCH, code)
            self.assertIn(f"HTTP {code}", check.detail)
            self.assertIn("unproven", check.detail)
        self.assertIn("gateway errored", self.check([self.delivery(2, 503, "2026-09-02T00:00:00Z")]).detail)

    def test_an_unreadable_delivery_list_is_unknown_not_green(self):
        check = self.check("not a list")
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("deliveries", check.detail)


class PingTest(Base):
    """`arm` proves each hook's secret with a GitHub ping; a ping itself never starts anything."""

    def setUp(self):
        super().setUp()
        from review_loop import hook_ping
        self.hook_ping = hook_ping
        self.fresh_install()
        self.ids = sorted(hook["id"] for hook in self.hooks())

    def test_arm_pings_every_loop_hook_and_reports_the_signature_accepted(self):
        rc, out = self.cli("arm", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(json.loads(t.WORLD_FILE.read_text())["pings"]), self.ids)
        for hook_id in self.ids:
            self.assertIn(f"hook {hook_id}: ping answered HTTP 200 — signature accepted", out)

    def test_a_rejected_ping_fails_arm_with_the_fix(self):
        self.world(ping_status={str(self.ids[0]): 401})
        rc, out = self.cli("arm", "--loop", "widgets")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"hook {self.ids[0]}: ping answered HTTP 401 — signature rejected", out)
        self.assertIn("uninstall --loop widgets", out)
        self.assertIn(f"hook {self.ids[1]}: ping answered HTTP 200", out)

    def test_no_delivery_within_the_wait_is_a_warning_not_a_pass(self):
        self.world(ping_silent=True)
        wait = self.hook_ping.PING_WAIT
        self.hook_ping.PING_WAIT = 0.2
        self.addCleanup(setattr, self.hook_ping, "PING_WAIT", wait)
        rc, out = self.cli("arm", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.assertIn("no ping delivery seen within 0.2s", out)
        self.assertNotIn("signature accepted", out)

    def test_init_arm_pings_the_hooks_it_created(self):
        rc, out = self.cli("uninstall", "--loop", "widgets")
        self.assertEqual(rc, 0, out)
        self.world(ping_status={str(self.ids[-1] + 1): 401})
        rc, out = self.init("--hooks", "--arm")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"hook {self.ids[-1] + 1}: ping answered HTTP 401", out)
        self.assertIn("init INCOMPLETE", out)

    def test_pause_sends_no_ping(self):
        self.cli("arm", "--loop", "widgets")
        self.world(pings=[])
        rc, out = self.cli("arm", "--loop", "widgets", "--pause")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(t.WORLD_FILE.read_text())["pings"], [])

    def test_the_gates_treat_a_ping_as_a_no_op(self):
        payload = {"zen": "Keep it logically awesome.", "hook_id": self.ids[0],
                   "hook": {"id": self.ids[0], "type": "Repository", "events": ["pull_request"]},
                   "repository": {"full_name": t.REPO}, "sender": {"login": "owner"}}
        state_before = sorted(str(p.relative_to(t.STATE_DIR)) for p in t.STATE_DIR.rglob("*"))
        for script in ("gate_reviewer.py", "gate_fixer.py"):
            kind, out, err = t.run(script, payload)
            self.assertEqual(kind, "SILENT", (script, out, err))
            self.assertNotIn("Traceback", err)
        state_after = sorted(str(p.relative_to(t.STATE_DIR)) for p in t.STATE_DIR.rglob("*"))
        self.assertEqual([p for p in state_after if "pending" in p or "queue" in p],
                         [p for p in state_before if "pending" in p or "queue" in p])

    def test_selftest_ping_never_pings_another_installs_hook(self):
        from review_loop import selftest
        loop = config.load_id("widgets")
        theirs = [{**hook, "config": {**hook["config"], "url": hook["config"]["url"].replace(
            t.HOST, "https://other-gateway.example")}} for hook in self.hooks()]
        self.world(hooks=theirs, pings=[])
        report = selftest.Report(out=io.StringIO())
        selftest.check_hook_signatures(report, loop, ping=True, login=t.FIXER)
        self.assertEqual(json.loads(t.WORLD_FILE.read_text())["pings"], [])
        self.assertEqual([r[2] for r in report.results], [selftest.FAIL])
        text = report.out.getvalue()
        self.assertIn("no repo hook posts to this loop's route URLs", text)
        self.assertIn("another origin (https://other-gateway.example", text)
        # Ours next to theirs (same route names): only ours is pinged.
        ours = [{**hook, "id": hook["id"] + 100, "config": {**hook["config"], "url": hook[
            "config"]["url"].replace("https://other-gateway.example", t.HOST)}} for hook in theirs]
        self.world(hooks=theirs + ours, pings=[])
        report = selftest.Report(out=io.StringIO())
        selftest.check_hook_signatures(report, loop, ping=True, login=t.FIXER)
        self.assertEqual(sorted(json.loads(t.WORLD_FILE.read_text())["pings"]),
                         sorted(hook["id"] for hook in ours))
        self.assertEqual({r[2] for r in report.results}, {selftest.PASS})

    def test_selftest_reads_evidence_and_pings_only_when_asked(self):
        from review_loop import selftest
        loop = config.load_id("widgets")
        report = selftest.Report(out=io.StringIO())
        with selftest.github_read_only():
            selftest.check_hook_signatures(report, loop, ping=False)
        self.assertEqual(json.loads(t.WORLD_FILE.read_text()).get("pings", []), [])
        self.assertEqual({r[2] for r in report.results}, {selftest.WARN})
        self.assertIn("unproven", report.out.getvalue())
        report = selftest.Report(out=io.StringIO())
        selftest.check_hook_signatures(report, loop, ping=True, login=t.FIXER)
        self.assertEqual({r[2] for r in report.results}, {selftest.PASS})
        self.assertEqual(sorted(json.loads(t.WORLD_FILE.read_text())["pings"]), self.ids)
        report = selftest.Report(out=io.StringIO())
        selftest.check_hook_signatures(report, loop, ping=False)
        self.assertEqual({r[2] for r in report.results}, {selftest.PASS})


class ConfigErrorTest(Base):
    def test_unknown_loop_is_a_clean_refusal_for_every_verb(self):
        for verb in ("status", "arm", "uninstall"):
            rc, out = self.cli(verb, "--loop", "wdigets")
            self.assertEqual(rc, 2, (verb, out))
            self.assertIn("no loop config named 'wdigets'", out)

    def test_list_and_status_skip_a_broken_file_and_show_the_rest(self):
        (t.LOOPS_DIR / "broken.json").write_text("{not json")
        for verb in ("list", "status"):
            rc, out = self.cli(verb)
            self.assertEqual(rc, 2, (verb, out))
            self.assertIn("skipping broken.json", out)
            self.assertIn("widgets", out)

    def test_init_refuses_ids_that_cannot_name_a_config_file(self):
        for bad in (".", "..", "a/b", ".hidden"):
            rc, out = self.cli("init", "--repo", t.REPO, "--id", bad, "--host", t.HOST,
                               "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                               "--reviewer-profile", "reviewer-profile",
                               "--fixer-profile", "fixer-profile")
            self.assertEqual(rc, 2, (bad, out))
        self.assertEqual(sorted(p.name for p in t.LOOPS_DIR.iterdir()), ["widgets.json"])
        rc, out = self.cli("list")
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
