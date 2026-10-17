"""#874: a seat's profile ``agent.reasoning_effort`` reaches its turns.

The sandboxed Hermes runs on a config.yaml the loop writes, and it had no ``agent:`` section, so
every turn ran at Hermes's default: the reviewer set to ``high`` reviewed at ``medium`` and the
fixer's subscription wire sent no reasoning at all (the ledger's thinking column, #513). The
profile's value is read host-side with the seat's model, carried on ``SeatInference``, written
into the sandbox config for every wire, and shown on doctor's ``model:<seat>`` line.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, seat_model, trusted_turn  # noqa: E402

WIRES = [("chat_completions", ""), ("chat_completions", "directsdk"),
         ("codex_responses", ""), ("anthropic_messages", ""),
         ("anthropic_messages", "claude_code")]


class SandboxConfig(unittest.TestCase):
    def test_every_wire_carries_the_seats_effort(self):
        for api_mode, identity in WIRES:
            with self.subTest(api_mode=api_mode, identity=identity):
                text, _, _ = trusted_turn.sandbox_config("m", api_mode, identity, "high")
                self.assertIn("agent:\n  reasoning_effort: high\n", text)
                unset, _, _ = trusted_turn.sandbox_config("m", api_mode, identity)
                self.assertNotIn("reasoning_effort", unset)       # Hermes's own default

    def test_none_turns_reasoning_off_and_a_bad_value_never_reaches_the_sandbox(self):
        text, _, _ = trusted_turn.sandbox_config("m", "chat_completions", "", "none")
        self.assertIn("reasoning_effort: none", text)
        for bad in ("hgih", "high\nplugins:", "x" * 40):
            with self.subTest(bad=bad), self.assertRaises(trusted_turn.TurnDenied):
                trusted_turn.sandbox_config("m", "chat_completions", "", bad)


class Resolution(unittest.TestCase):
    """Host-side: the resolver's answer carries the profile's effort onto the seat."""

    def resolve(self, **answer):
        base = {"model": "m", "requested": "deepseek", "provider": "deepseek",
                "api_mode": "chat_completions", "base_url": "https://api.example/v1",
                "key": "k", "auth": "api_key"}
        with mock.patch.object(seat_model, "profile_problem", return_value=""), \
                mock.patch.object(seat_model, "run_resolver", return_value={**base, **answer}):
            return seat_model.resolve_profile("p", "reviewer", {})

    def test_the_profiles_effort_is_on_the_seat(self):
        self.assertEqual(self.resolve(effort="high").reasoning_effort, "high")
        self.assertEqual(self.resolve().reasoning_effort, "")

    def test_an_effort_hermes_would_not_accept_holds_the_seat(self):
        with self.assertRaisesRegex(seat_model.SeatModelError, "reasoning_effort 'hgih'"):
            self.resolve(effort="hgih")

    def test_a_subscription_process_seat_carries_it_too(self):
        from diaktoros.directsdk_backend import PROVIDER, UPSTREAM
        seat = self.resolve(requested=PROVIDER, provider=PROVIDER, base_url=UPSTREAM,
                            key="host-process", auth="external_process", effort="high")
        self.assertEqual((seat.client_identity, seat.reasoning_effort), ("directsdk", "high"))


class Doctor(unittest.TestCase):
    def describe(self, effort):
        loop = config.normalize({"id": "w", "repo": "acme/widgets", "fixers": ["f"],
                                 "reviewers": ["r"], "read_token": "reader",
                                 "seats": {"reviewer": {"profile": "rev", "route": "r"},
                                           "fixer": {"profile": "fix", "route": "f"}}})
        answer = {"model": "m", "requested": "deepseek", "base_url": "https://api.example/v1",
                  "api_mode": "chat_completions", "effort": effort,
                  "hermes": {"api_mode": "chat_completions", "auth": "api_key"}}
        with mock.patch.object(seat_model, "run_resolver", return_value=answer), \
                mock.patch.object(seat_model, "profile_problem", return_value=""):
            return seat_model.describe_seat(loop, "reviewer", {})

    def test_the_model_line_names_the_effort(self):
        status, detail, _ = self.describe("high")
        self.assertEqual(status, "ok", detail)
        self.assertIn("reasoning high", detail)
        self.assertIn("reasoning Hermes default (unset)", self.describe("")[1])

    def test_a_bad_effort_fails_the_model_line(self):
        status, detail, fix = self.describe("hgih")
        self.assertEqual(status, "fail")
        self.assertIn("reasoning_effort 'hgih'", detail)


import test_pacing as tp  # noqa: E402


class Worker(unittest.TestCase):
    """The real ``_run_production`` hands the resolved seat's effort to the turn. Borrows
    test_pacing's worker harness without inheriting (and rerunning) its tests."""

    setUp = tp.ProductionWorker.setUp
    run_production = tp.ProductionWorker.run_production

    def test_the_turn_gets_the_seats_effort(self):
        seen = {}

        def turn(_loop, scope, observed=None, **kwargs):
            seen["effort"] = kwargs.get("reasoning_effort")
            return 0
        self.inference.reasoning_effort = "high"
        self.run_production(turn)
        self.assertEqual(seen["effort"], "high")
        self.inference.reasoning_effort = ""
        seen.clear()
        self.run_production(turn)
        self.assertEqual(seen.get("effort"), "")


if __name__ == "__main__":
    unittest.main()
