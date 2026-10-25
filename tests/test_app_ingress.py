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
        self.addCleanup(lambda: routes.restore_entries({"dk-app-acme-reader": None}))

    def loop(self):
        return config.load_id("widgets")

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
        self.assertIn(f"hooks                none, on purpose: events arrive through {READ_APP}", out)

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
