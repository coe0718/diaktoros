"""#1599 (part 2 of #677): ``hermes dk onboard`` shows the onboarding record and the next command.

Driven through ``cmd_onboard`` (the parser's own dispatch). The record is written with
``onboarding.record``; the seat resolution, the PR head and the hook state are patched, so no test
reaches GitHub or a model. The command must write nothing and never call ``arm`` or ``fixer-push``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_reader_identity import _Loop  # noqa: E402
from diaktoros import config, gh, onboarding, seat_model  # noqa: E402
from diaktoros.cli import checks as cli_checks, identity_command  # noqa: E402

HEAD = "a" * 40
_REAL = {"_live_head": onboarding._live_head, "_hook_state": onboarding._hook_state}
STAGES = onboarding.STAGES


class Onboard(_Loop):
    def setUp(self):
        super().setUp()
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        self.loop = config.load_id("widgets")
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(json.dumps(
            {"source": "/s", "venv": "/v", "runtime": "/r", "rust": "/c"}))
        self.head, self.hooks, self.model = HEAD, "armed", "model-a"
        # Every real default the command reads: the runtime file, the seats, GitHub.
        for target, value in (
                (config, ("host_path", lambda key, base=None: self.runtime)),
                (seat_model, ("resolve_seat", self._resolve)),
                (onboarding, ("_live_head", lambda loop, number: self.head)),
                (onboarding, ("_hook_state", lambda loop: self.hooks))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _resolve(self, loop, seat, settings, resolver=None):
        return seat_model.SeatInference(seat, "p", "profile", "prov", self.model,
                                        "https://api.example/v1/chat/completions", "k" * 12)

    def record(self, stage, status="passed", **kw):
        if stage == "live_turn":
            kw.setdefault("pr", (12, self.head))
        return onboarding.record(self.loop, stage, status, f"hermes dk {stage}",
                                 f"{stage} summary", runtime_file=self.runtime,
                                 hook_state=lambda loop: self.hooks, **kw)

    def onboard(self, *extra):
        rc, out = self.run_cli(["onboard", "--loop", "widgets", *extra])
        self.assertEqual(rc, 0, out)
        return out

    def snapshot(self):
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in sorted(self.home.rglob("*")) if p.is_file()}

    def next_line(self, out):
        return next(line for line in out.splitlines() if line.startswith("next:"))

    def test_a_fresh_install_shows_identities_next_and_the_rest_not_attempted(self):
        out = self.onboard()
        for stage in STAGES:
            self.assertRegex(out, rf"{stage}\s+not_attempted")
        self.assertIn("hermes dk selftest --loop widgets --no-model", self.next_line(out))
        self.assertIn("identities stage", self.next_line(out))
        self.assertIn("live read", self.next_line(out))

    def test_partial_progress_names_the_first_stage_that_is_not_passed(self):
        self.record("identities")
        self.record("loop")
        out = self.onboard()
        self.assertRegex(out, r"identities\s+passed")
        self.assertRegex(out, r"model\s+not_attempted")
        line = self.next_line(out)
        self.assertEqual(line.split("(")[0].strip(), "next: hermes dk selftest --loop widgets")
        self.assertIn("model-spending", line)
        self.assertIn("recorded ", out)
        self.assertIn("by `hermes dk identities`", out)
        self.assertIn("identities summary", out)

    def test_a_failed_stage_is_next_and_the_live_turn_and_hook_stages_are_labelled(self):
        for stage in ("identities", "loop", "model"):
            self.record(stage)
        line = self.next_line(self.onboard("--pr", "7"))
        self.assertIn("selftest --loop widgets --pr 7 --live-turn", line)
        self.assertIn("live turn", line)
        self.record("live_turn")
        line = self.next_line(self.onboard())
        self.assertIn("hermes dk arm --loop widgets", line)
        self.assertIn("hook write", line)

    def test_a_stale_stage_is_shown_stale_with_the_fingerprint_that_changed(self):
        for stage in STAGES:
            self.record(stage)
        self.assertIn("next: nothing", self.next_line(self.onboard()))
        self.model = "model-b"
        out = self.onboard()
        self.assertRegex(out, r"identities\s+passed")
        self.assertRegex(out, r"model\s+stale")
        self.assertRegex(out, r"live_turn\s+stale")
        self.assertRegex(out, r"armed\s+stale")
        self.assertIn("model fingerprint changed (a seat's model, endpoint or key)", out)
        self.assertIn("model is stale, and this was shown against it", out)
        self.assertIn("hermes dk selftest --loop widgets", self.next_line(out))
        self.assertIn("model-spending", self.next_line(out))
        self.head = "b" * 40
        self.model = "model-a"
        self.assertRegex(self.onboard(), r"live_turn\s+stale")
        self.assertIn("live_turn fingerprint changed (the PR's head", self.onboard())

    def test_an_unresolved_seat_never_reads_passed(self):
        self.record("model")
        with patch.object(seat_model, "resolve_seat",
                          side_effect=seat_model.SeatModelError("profile unreadable")):
            out = self.onboard()
        self.assertRegex(out, r"model\s+stale")
        self.assertIn("seat model unresolved", out)

    def test_it_writes_no_file_and_never_arms_pushes_or_writes_to_github(self):
        for stage in STAGES:
            self.record(stage)
        before = self.snapshot()
        methods = []

        def request(loop, path, method="GET", *a, **kw):
            methods.append(method)
            raise AssertionError("onboard reached GitHub")
        refuse = AssertionError("onboard must not run this")
        with patch.object(gh, "request", request), \
                patch.object(cli_checks, "cmd_arm", side_effect=refuse), \
                patch.object(cli_checks.arm_action, "perform", side_effect=refuse), \
                patch.object(identity_command, "cmd_fixer_push", side_effect=refuse), \
                patch.object(config, "_write_loop", side_effect=refuse, create=True):
            out = self.onboard()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(methods, [])
        self.assertIn("unattended fixer pushes: off", out)
        self.assertIn("fixer-push --loop widgets --enable --acknowledge-pr-race", out)

    def real(self):
        """The real GitHub-reading defaults setUp replaced (the transport boundary)."""
        for name in ("_live_head", "_hook_state"):
            patcher = patch.object(onboarding, name, _REAL[name])
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_an_app_reader_is_never_asked_to_mint_a_token_by_onboard(self):
        """F1: reading as an App mints an installation token (a POST); onboard must not."""
        from diaktoros import github_app
        loop = dict(self.loop, read_token="reader-app",
                    apps={"reader-app": {"app_id": 7, "key_file": str(self.root / "k.pem")}})
        github_app.forget()
        self.addCleanup(github_app.forget)
        for stage in ("identities", "loop", "model"):   # recorded against this loop's App mapping
            onboarding.record(loop, stage, "passed", f"dk {stage}", "ok",
                              runtime_file=self.runtime)
        onboarding.record(loop, "live_turn", "passed", "dk selftest", "ok",
                          pr=(12, self.head), runtime_file=self.runtime)
        calls = []

        def as_app(lp, login, path, method="GET", body=None):
            calls.append((method, path))
            return gh.Response(None, "refused")
        self.real()
        with patch.object(config, "load_id", return_value=loop), \
                patch.object(github_app, "_as_app", side_effect=as_app):
            out = self.onboard()
        self.assertEqual(calls, [], "onboard reached the App token path")
        self.assertRegex(out, r"live_turn\s+stale")

    def test_a_corrupt_app_ingress_pause_flag_never_reads_armed(self):
        """F2: an unreadable pause flag is unknown, so a recorded `armed` goes stale."""
        from diaktoros import app_ingress, state as state_mod
        loop = dict(self.loop, ingress="app-webhook", ingress_app="reader-acct")
        flag = state_mod.state_for(loop).dir / app_ingress.FILE
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps({"paused": False}))
        self.real()
        entry = onboarding.record(loop, "armed", "passed", "dk arm", "ok",
                                  runtime_file=self.runtime)
        self.assertEqual(entry["status"], "passed")
        for bad in ("{not json", json.dumps({"paused": "no"})):
            flag.write_text(bad)
            with patch.object(config, "load_id", return_value=loop):
                self.assertRegex(self.onboard(), r"armed\s+stale")
        entry = onboarding.record(loop, "armed", "passed", "dk arm", "ok",
                                  runtime_file=self.runtime)
        self.assertEqual((entry["status"], entry["fingerprint"]), ("failed", ""))

    def test_it_reports_unattended_fixer_pushes_on(self):
        loop = dict(self.loop, unattended_fixer_push=True)
        with patch.object(config, "load_id", return_value=loop):
            self.assertIn("unattended fixer pushes: on", self.onboard())

    def test_it_has_no_run_flag_and_an_unknown_loop_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["onboard", "--loop", "widgets", "--run"])
        rc, out = self.run_cli(["onboard", "--loop", "nope"])
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot show onboarding", out)


if __name__ == "__main__":
    unittest.main()
