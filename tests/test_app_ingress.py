"""#1239: a loop whose events arrive through a GitHub App's own webhook. The App's route is shared
by every loop it delivers for; its dispatcher picks the gate from the payload's shape; the loop has
no repo hooks, and pausing it is a flag its dispatcher reads."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import app_ingress, config, routes, state as state_mod  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402

READ_APP = "acme-reader[bot]"


class Flow(app_cli.Base):
    def setUp(self):
        super().setUp()
        self.key_read = self.key("read")
        rc, out = self.run_cli(self.app_argv(
            "--ingress", "app-webhook", "--ingress-app", READ_APP, reader=READ_APP,
            tokens=[(tfs.FIX, self.pats["fix"])],
            apps=[(app_cli.BOT_REV, 5416187, self.key("rev")), (READ_APP, 5416115, self.key_read)]))
        self.assertEqual(rc, 0, out)
        self.init_out = out
        self.addCleanup(lambda: routes.restore_entries({"dk-app-acme-reader": None}))
        # The App's webhook as GitHub answers it, read with the App's JWT: pointed at the route,
        # with the events the loop needs, unless a test says otherwise.
        from diaktoros import github_app, gh
        self.webhook = {"url": routes.target("dk-app-acme-reader", "https://gateway.example")[0],
                        "content_type": "json", "events": ["pull_request", "pull_request_review"],
                        "error": ""}

        def as_app(loop, login, path, method="GET", body=None):
            if self.webhook["error"]:
                return gh.Response(None, self.webhook["error"], 401, {})
            if path == "/app/hook/config":
                return gh.Response({k: self.webhook[k] for k in ("url", "content_type")}, "", 200, {})
            return gh.Response({"events": self.webhook["events"]}, "", 200, {})
        patch = mock.patch.object(github_app, "_as_app", side_effect=as_app)
        patch.start()
        self.addCleanup(patch.stop)

    def loop(self):
        return config.load_id("widgets")

    def test_doctor_reads_the_apps_webhook(self):
        from diaktoros import doctor
        from diaktoros.doctor import hook_checks
        for change, status, words in (
                ({}, doctor.VERIFIED, "posts to https://gateway.example/webhooks/dk-app-acme-reader"),
                ({"url": "https://example.com/dk-spike"}, doctor.MISMATCH,
                 "posts to https://example.com/dk-spike, not the route's"),
                ({"events": ["pull_request"]}, doctor.MISMATCH,
                 "does not subscribe to pull_request_review"),
                ({"content_type": "form"}, doctor.MISMATCH, "content type is form"),
                ({"error": "HTTP 401"}, doctor.UNKNOWN, "could not be read")):
            with self.subTest(change=change):
                saved = dict(self.webhook)
                self.webhook.update(change)
                check = hook_checks.check_app_webhook(self.loop(), offline=False)
                self.webhook.clear()
                self.webhook.update(saved)
                self.assertEqual(check.status, status, check)
                self.assertIn(words, check.detail)
                if status == doctor.MISMATCH:
                    self.assertIn("hermes dk app-webhook --loop widgets --set", check.fix)

    def test_doctor_and_explain_carry_the_apps_lines(self):
        from diaktoros import doctor
        from diaktoros.doctor import hook_checks
        with mock.patch.object(doctor.gh, "api", return_value=[]):
            names = [c.name for c in hook_checks.check_hooks(self.loop(), offline=False)]
        self.assertEqual(names, ["hooks", "app-webhook"])
        rc, out = self.run_cli(["explain", "--loop", "widgets", "--pr", "7"])
        self.assertIn("armed — events arrive through acme-reader[bot]'s webhook", out)
        self.assertNotIn("seat routes are active repo hooks", out)

    def test_arm_refuses_while_the_routes_script_cannot_run_and_apply_writes_it(self):
        from diaktoros import gate_shims
        entry = routes.route("dk-app-acme-reader")
        found, _ = gate_shims.resolve(gate_shims.home_for(entry["profile"]), entry["script"])
        found.unlink()
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("cannot run route dk-app-acme-reader's script", out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)                   # the advice holds

    def test_status_names_the_app_route(self):
        rc, out = self.run_cli(["status", "--loop", "widgets"])
        self.assertIn("app dk-app-acme-reader → default (ok)", out)

    def test_arm_needs_the_webhook_to_deliver_and_explain_names_it(self):
        st = state_mod.state_for(self.loop())
        self.run_cli(["arm", "--loop", "widgets", "--pause"])
        self.webhook["url"] = "https://example.com/dk-spike"
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("would deliver nothing to dk-app-acme-reader", out)
        self.assertTrue(app_ingress.paused(st))           # still paused: nothing was switched
        from diaktoros import explain
        self.assertIn("PAUSED — events arrive through acme-reader[bot]'s webhook",
                      explain._explain_app_ingress(self.loop(), False))
        self.webhook["url"] = routes.target("dk-app-acme-reader", "https://gateway.example")[0]
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(explain._explain_app_ingress(self.loop(), True),
                         "armed — events arrive through acme-reader[bot]'s webhook "
                         "(`hermes dk doctor` reads that webhook)")

    def test_the_dispatcher_runs_the_real_gate_under_its_own_name(self):
        # The gateway's way in: gate_app.py as __main__, the payload on stdin. The reviewer gate
        # it starts stops at its first GitHub read (no network here), recorded under its name.
        import os
        import subprocess
        root = Path(__file__).resolve().parents[1]
        payload = json.dumps({"action": "opened", "number": 7, "pull_request": {
            "number": 7, "head": {"sha": "a" * 40}, "base": {"ref": "main"},
            "user": {"login": tfs.FIX}}, "repository": {"full_name": "acme/widgets"},
            "sender": {"login": tfs.FIX}})
        st_dir = state_mod.state_for(self.loop()).dir
        proc = subprocess.run([sys.executable, str(root / "scripts" / "gate_app.py")],
                              input=payload, capture_output=True, text=True, timeout=60,
                              cwd=str(root / "scripts"), env=dict(os.environ))
        self.assertNotIn("Traceback", proc.stderr, proc.stderr)
        rows = [json.loads(line) for line in
                (st_dir / "gate-decisions.jsonl").read_text().splitlines() if line.strip()] \
            if (st_dir / "gate-decisions.jsonl").exists() else []
        failures = (st_dir / "gate-failures.json").read_text() \
            if (st_dir / "gate-failures.json").exists() else ""
        self.assertTrue(any(row.get("gate") == "gate_reviewer" for row in rows)
                        or "gate_reviewer" in failures, (rows, failures, proc.stdout, proc.stderr))
        self.assertFalse(any(row.get("gate") == "gate_app" for row in rows), rows)

    def test_init_says_to_point_the_apps_webhook(self):
        self.assertIn("next: hermes dk app-webhook --loop widgets --set", self.init_out)
        self.assertNotIn("pass --hooks", self.init_out)

    def test_init_installs_the_apps_route_and_no_hooks(self):
        loop = self.loop()
        self.assertEqual((loop["ingress"], loop["ingress_app"]), ("app-webhook", READ_APP))
        self.assertEqual(config.app_route(loop), "dk-app-acme-reader")
        self.assertEqual(config.hook_roles(loop), ())
        entry = routes.route("dk-app-acme-reader")
        self.assertEqual(entry["script"], "gate_app.py")
        self.assertEqual(entry["profile"], "default")
        self.assertTrue({"pull_request", "pull_request_review", "issues"} <= set(entry["events"]))

    def test_an_app_route_missing_an_event_is_drift(self):
        from diaktoros import gate_shims
        entry = dict(routes.route("dk-app-acme-reader"))
        self.assertEqual(gate_shims.contract_drift(self.loop(), "app", entry), [])
        for event in ("pull_request", "pull_request_review", "issues"):
            with self.subTest(event=event):
                bad = {**entry, "events": [e for e in entry["events"] if e != event]}
                self.assertIn("events", gate_shims.contract_drift(self.loop(), "app", bad))

    def test_the_read_commands_take_an_app_loop(self):
        for argv in (["show", "--loop", "widgets"], ["status", "--loop", "widgets"],
                     ["doctor", "--loop", "widgets", "--offline"]):
            with self.subTest(argv=argv[0]):
                rc, out = self.run_cli(argv)
                self.assertNotIn("Traceback", out)
                self.assertNotIn("KeyError", out)
                self.assertNotIn("route:dk-app", out.replace("✅ route:dk-app", ""), out)  # app route checks out
        rc, out = self.run_cli(["show", "--loop", "widgets"])
        self.assertIn(f"events arrive through {READ_APP}'s webhook", out)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline"])
        self.assertIn(f"not probed (--offline): events arrive through {READ_APP}'s webhook", out)

    def test_doctor_names_a_leftover_repo_hook_to_a_seat_route(self):
        from diaktoros import doctor, route_intent
        from diaktoros.doctor import hook_checks
        loop = self.loop()
        seat = route_intent.routes_of(loop)["reviewer"]
        other = {"id": 9, "config": {"url": "https://gateway.example/webhooks/someone-else"},
                 "events": ["push"], "active": True}
        left = {"id": 41, "config": {"url": f"https://gateway.example/webhooks/{seat}"},
                "events": ["pull_request"], "active": True}
        for listing, status, words in (([other], doctor.VERIFIED, "none, on purpose"),
                                       ([other, left], doctor.MISMATCH, "(id 41)"),
                                       ({"message": "Not Found"}, doctor.UNKNOWN, "not looked for")):
            with self.subTest(status=status), \
                    mock.patch.object(doctor.gh, "api", return_value=listing):
                check = hook_checks.check_leftover_hooks(loop, offline=False)
            self.assertEqual(check.status, status, check)
            self.assertIn(words, check.detail)

    def test_a_gate_woken_straight_from_a_repo_hook_drops_the_delivery(self):
        # The gateway runs a seat route's script as __main__: that is a leftover repo hook on a
        # loop whose events come through the App. Run the real script the way the gateway does.
        import os
        import subprocess
        from diaktoros import gate_failures
        root = Path(__file__).resolve().parents[1]
        payload = json.dumps({"action": "opened", "number": 7, "pull_request": {
            "number": 7, "head": {"sha": "a" * 40}, "base": {"ref": "main"},
            "user": {"login": tfs.FIX}}, "repository": {"full_name": "acme/widgets"},
            "sender": {"login": tfs.FIX}})
        decisions = state_mod.state_for(self.loop()).dir / "gate-decisions.jsonl"
        env = {k: v for k, v in os.environ.items() if k != gate_failures.REDRIVE_ENV}
        for redrive in (False, True):
            with self.subTest(redrive=redrive):
                decisions.unlink(missing_ok=True)
                proc = subprocess.run(
                    [sys.executable, str(root / "scripts" / "gate_reviewer.py")], input=payload,
                    capture_output=True, text=True, timeout=60, cwd=str(root / "scripts"),
                    env={**env, **({gate_failures.REDRIVE_ENV: "k"} if redrive else {})})
                self.assertIn("[SILENT]", proc.stdout, proc.stderr)
                text = decisions.read_text() if decisions.exists() else ""
                # A watchdog re-drive of a delivery the dispatcher once took runs as __main__
                # too, with its mark: never dropped as a leftover.
                self.assertEqual("leftover repo hook" in text, not redrive, text)
        self.assertFalse(app_ingress.woken_by_repo_hook())   # in-process (CLI, dispatcher)

    def test_arm_refuses_while_the_apps_route_is_missing_and_repair_restores_it(self):
        routes.restore_entries({"dk-app-acme-reader": None})
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("not armed: the App's route dk-app-acme-reader is not installed", out)
        from diaktoros import doctor
        from diaktoros.doctor import hook_checks
        with mock.patch.object(doctor.gh, "api", return_value=[]):     # no hooks: still not green
            check = hook_checks.check_leftover_hooks(self.loop(), offline=False)
        self.assertEqual(check.status, doctor.UNKNOWN, check)
        self.assertIn("that route is not installed", check.detail)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline", "--repair"])
        self.assertIsNotNone(routes.route("dk-app-acme-reader"), out)   # the advice holds
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)

    def test_selftest_skips_the_repo_hook_step_and_uninstall_sees_leftovers(self):
        from diaktoros import route_intent, selftest
        report = selftest.Report(out=io.StringIO())
        with mock.patch.object(selftest.gh, "hooks_read") as read:
            selftest.check_hook_signatures(report, self.loop())
        read.assert_not_called()
        self.assertEqual([r[2] for r in report.results], [selftest.SKIP])
        seats = route_intent.routes_of(self.loop())
        self.assertEqual(cli_common._hook_route_names(self.loop()),
                         {seats["reviewer"], seats["fixer"]})

    def test_arm_and_pause_switch_the_flag(self):
        st = state_mod.state_for(self.loop())
        rc, out = self.run_cli(["arm", "--loop", "widgets", "--pause"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(app_ingress.paused(st))
        self.assertIn("dropped before any gate runs", out)
        from diaktoros import gate
        self.assertEqual(gate.hooks_read(self.loop())[0], False)
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(app_ingress.paused(st))
        self.assertEqual(gate.hooks_read(self.loop())[0], True)

    def test_a_second_loop_shares_the_route_and_uninstall_keeps_it_for_the_other(self):
        other = {**self.loop(), "id": "gadgets", "repo": "acme/gadgets",
                 "state_dir": str(Path(self.loop()["state_dir"]).parent / "gadgets"),
                 "seats": {seat: {**cfg, "route": cfg["route"].replace("widgets", "gadgets")}
                           for seat, cfg in self.loop()["seats"].items()}}
        cli_common._verify_routes(other, ("app",))          # the same App's route: no collision
        with self.assertRaisesRegex(config.ConfigError, "same --host"):
            cli_common._verify_routes({**other, "host": "https://elsewhere.example"}, ("app",))
        cli_common._write_config(config.normalize(other))
        rc, out = self.run_cli(["uninstall", "--loop", "widgets"])
        self.assertIn("route kept: dk-app-acme-reader", out)
        self.assertIsNotNone(routes.route("dk-app-acme-reader"))

    def test_app_webhook_shows_and_sets_the_apps_webhook(self):
        from diaktoros import github_app, gh
        calls, state = [], {"url": "https://example.com/old", "events": ["pull_request"]}

        def as_app(loop, login, path, method="GET", body=None):
            calls.append((login, path, method, body))
            if path == "/app/hook/config" and method == "PATCH":
                state["url"] = body["url"]
                return gh.Response({"url": body["url"], "content_type": "json"}, "", 200, {})
            if path == "/app/hook/config":
                return gh.Response({"url": state["url"], "content_type": "json"}, "", 200, {})
            return gh.Response({"events": state["events"]}, "", 200, {})
        with mock.patch.object(github_app, "_as_app", side_effect=as_app):
            rc, out = self.run_cli(["app-webhook", "--loop", "widgets"])
            self.assertEqual(rc, 1, out)
            self.assertIn("not the route", out)
            self.assertIn("missing pull_request_review", out)
            self.assertIn("hermes dk app-webhook --loop widgets --set", out)
            state["events"] = ["pull_request", "pull_request_review"]
            rc, out = self.run_cli(["app-webhook", "--loop", "widgets", "--set"])
        self.assertEqual(rc, 0, out)
        self.assertIn("matches the route", out)
        [(login, _, _, body)] = [c for c in calls if c[2] == "PATCH"]
        self.assertEqual(login, READ_APP)
        url, secret = routes.target("dk-app-acme-reader", self.loop().get("host"))
        self.assertEqual((body["url"], body["secret"], body["content_type"]),
                         (url, secret.decode() if isinstance(secret, bytes) else secret, "json"))
        self.assertNotIn(body["secret"], out)                          # never printed

    def test_app_webhook_needs_a_mapped_app(self):
        loop = self.loop()
        for bad in ({"ingress_app": ""}, {"ingress_app": "not-mapped[bot]"},
                    {"ingress_app": tfs.FIX}):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "ingress_app"):
                config.normalize({**loop, **bad})
        with self.assertRaisesRegex(config.ConfigError, "'ingress' must be one of"):
            config.normalize({**loop, "ingress": "smoke-signals"})


class Switch(app_cli.Base):
    def test_set_moves_a_repo_hooks_loop_onto_the_apps_webhook(self):
        rc, out = self.run_cli(self.app_argv(
            reader=READ_APP, tokens=[(tfs.FIX, self.pats["fix"])],
            apps=[(app_cli.BOT_REV, 5416187, self.key("rev")), (READ_APP, 5416115, self.key("read"))]))
        self.assertEqual(rc, 0, out)
        self.addCleanup(lambda: routes.restore_entries({"dk-app-acme-reader": None}))
        self.assertIsNone(routes.route("dk-app-acme-reader"))
        rc, out = self.run_cli(["set", "--loop", "widgets", "--ingress", "app-webhook",
                                "--ingress-app", READ_APP])
        self.assertEqual(rc, 0, out)
        entry = routes.route("dk-app-acme-reader")
        self.assertEqual(entry["script"], "gate_app.py")
        from diaktoros import route_intent
        self.assertIn("dk-app-acme-reader", json.dumps(route_intent.load(config.load_id("widgets"))))
        self.assertIn("route written: dk-app-acme-reader", out)
        from diaktoros import gate_shims
        found, why = gate_shims.resolve(gate_shims.home_for(entry["profile"]), entry["script"])
        self.assertIsNotNone(found, why)               # the gateway can run the route's script
        self.assertEqual([c for c in gate_shims.live_checks(config.load_id("widgets"))
                          if c[1] == "absent"], [])
        self.assertIn("app-webhook --loop widgets --set", out)
        self.assertIn("the gates now drop what they deliver", out)


class InitHooks(app_cli.Base):
    def test_init_hooks_on_an_app_loop_promises_no_hooks(self):
        rc, out = self.run_cli(self.app_argv(
            "--ingress", "app-webhook", "--ingress-app", READ_APP, "--hooks",
            reader=READ_APP, tokens=[(tfs.FIX, self.pats["fix"])],
            apps=[(app_cli.BOT_REV, 5416187, self.key("rev")), (READ_APP, 5416115, self.key("read"))]))
        self.addCleanup(lambda: routes.restore_entries({"dk-app-acme-reader": None}))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("created paused", out)
        self.assertNotIn("the hooks are ARMED", out)
        self.assertIn("next: hermes dk app-webhook --loop widgets --set", out)


class Dispatch(unittest.TestCase):
    def test_the_gate_is_read_from_the_payloads_shape(self):
        pr = {"number": 7}
        repo = {"full_name": "acme/widgets"}
        for payload, want in (
                ({"action": "submitted", "review": {}, "pull_request": pr, "repository": repo}, "gate_fixer"),
                ({"action": "opened", "pull_request": pr, "repository": repo}, "gate_reviewer"),
                ({"action": "opened", "issue": {}, "repository": repo}, "gate_triage"),
                ({"zen": "Keep it simple.", "hook_id": 1}, ""),
                ({"action": "created", "comment": {}, "issue": {}, "repository": repo}, ""),
                ({"action": "created", "installation": {}}, ""),
                ({"ref": "refs/heads/main", "repository": repo}, "")):
            with self.subTest(want=want, keys=sorted(payload)):
                self.assertEqual(app_ingress.gate_for(payload)[0], want)

    def run_dispatch(self, payload, loop):
        from scripts import gate_app
        self.addCleanup(setattr, sys, "argv", list(sys.argv))   # the dispatcher renames argv[0]
        out = io.StringIO()
        with mock.patch.object(gate_app.sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(gate_app.config, "loop_for_repo", return_value=loop), \
                mock.patch.object(gate_app.gate_failures if hasattr(gate_app, "gate_failures")
                                  else __import__("diaktoros.gate_failures").gate_failures,
                                  "run") as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) if loop is None or not config.app_ingress(loop) \
                or loop.get("_paused") else contextlib.nullcontext():
            gate_app.main()
        return run, out.getvalue()

    def test_a_repo_hooks_loop_or_a_paused_loop_is_not_woken(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        payload = {"action": "opened", "pull_request": {"number": 7},
                   "repository": {"full_name": "acme/widgets"}}
        hooks_loop = {"id": "w", "repo": "acme/widgets", "ingress": "repo-hooks", "ingress_app": "",
                      "state_dir": tmp.name}
        run, out = self.run_dispatch(payload, hooks_loop)
        run.assert_not_called()
        self.assertIn("[SILENT]", out)
        app_loop = {**hooks_loop, "ingress": "app-webhook", "ingress_app": READ_APP}
        app_ingress.set_paused(state_mod.state_for(app_loop), True)
        run, out = self.run_dispatch(payload, {**app_loop, "_paused": True})
        run.assert_not_called()
        app_ingress.set_paused(state_mod.state_for(app_loop), False)
        run, out = self.run_dispatch(payload, app_loop)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "gate_reviewer")
        self.assertTrue(sys.argv[0].endswith("gate_reviewer.py"))   # decisions name the real gate


if __name__ == "__main__":
    unittest.main()
