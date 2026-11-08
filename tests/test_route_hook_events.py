"""Every seat's route carries its webhook events (#681, #680, #683), not only the fixer's.

``_install_routes`` wrote the fixer's events from ``config.hook_events`` but hardcoded the
reviewer's (``pull_request``) and triage's (``issues``). So ``set --protection-webhook on`` or
``--comment-webhook on`` and then ``apply --hooks`` rewrote the route without the event, its
readback still differed, and the whole apply rolled back.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gate_shims as gs  # noqa: E402
from diaktoros import config  # noqa: E402
from diaktoros.doctor import route_checks  # noqa: E402

ROUTES = {"reviewer": "widgets-review", "fixer": "widgets-fix", "triage": "widgets-triage"}
SWITCHES = ("--ci-webhook", "--comment-webhook", "--protection-webhook")


class EverySeatRoute(gs.Base):
    world, hook = gs.HookAndCronRemedies.world, staticmethod(gs.HookAndCronRemedies.hook)

    def enable_triage(self):
        rc, out = self.run_cli(["triage", "--loop", "widgets", "--enable", "--triage-profile",
                                "critic", "--author", "Owner", "--labels", "bug,docs,P1"])
        self.assertEqual(rc, 0, out)

    def assert_routes_carry_their_events(self):
        loop, live = config.load_id("widgets"), self.loop_routes()
        for role, name in ROUTES.items():
            with self.subTest(role=role):
                self.assertEqual(live[name]["events"], config.hook_events(loop, role))
        checks = {c.name: c for c in route_checks.check_routes(loop)}
        for name in ROUTES.values():
            self.assertFalse(checks[f"route:{name}"].failed, checks[f"route:{name}"].detail)

    def test_set_all_three_on_then_apply_hooks_rewrites_every_route(self):
        self.install()
        self.enable_triage()
        url = "https://gateway.example/p/{}/webhooks/{}"
        world = self.world([self.hook(1, url.format("critic", ROUTES["reviewer"]), ["pull_request"]),
                            self.hook(2, url.format("coder", ROUTES["fixer"]),
                                      ["pull_request_review"]),
                            self.hook(3, url.format("critic", ROUTES["triage"]), ["issues"])])
        argv = ["set", "--loop", "widgets"]
        for switch in SWITCHES:
            argv += [switch, "on"]
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--hooks"])
        self.assertNotIn("reconciliation FAILED", out)
        self.assertEqual(rc, 0, out)
        loop = config.load_id("widgets")
        self.assertEqual(config.hook_events(loop, "reviewer"),
                         ["pull_request", *config.PROTECTION_EVENTS])
        self.assertEqual(config.hook_events(loop, "triage"), ["issues", config.COMMENT_EVENT])
        self.assert_routes_carry_their_events()
        live = {h["id"]: h["events"] for h in json.loads(world.read_text())["hooks"]}
        self.assertEqual(live, {1: config.hook_events(loop, "reviewer"),
                                2: config.hook_events(loop, "fixer"),
                                3: config.hook_events(loop, "triage")})

    def test_a_new_loop_with_them_on_writes_them_on_every_route(self):
        self.install("acme/widgets", *[a for s in SWITCHES for a in (s, "on")])
        self.enable_triage()
        self.assert_routes_carry_their_events()
        self.assertIn("branch_protection_rule", self.loop_routes()["widgets-review"]["events"])
        self.assertIn("issue_comment", self.loop_routes()["widgets-triage"]["events"])


if __name__ == "__main__":
    unittest.main()
