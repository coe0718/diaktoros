#!/usr/bin/env python3
"""#981: ``apply`` never says "already matches" over a settings-form key it would change.

``_apply`` decides "nothing to do" from hand-kept lists of the keys it compares. A form key added
to ``settings_form.apply_settings`` and left out of those lists would be dropped silently (#781).
This test walks *every* key in ``SETTINGS_SCHEMA`` — a new key without a row in ``PUSHES`` fails
``test_every_schema_key_has_a_push`` — pushes it alone with a valid non-default value through the
real ``apply`` command, and asserts that whenever the pushed loop differs from the loaded one,
apply does not print "already matches" and writes the change.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_token_file_settings as tf  # noqa: E402
from diaktoros import config, routes  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

# One valid, non-default value per form key, each different from what the fixture loop holds.
# A callable gets the test case (token files live in its temp home).
PUSHES = {
    "cap": "4",
    "reviewer_concurrency": "2",
    "fixer_concurrency": "2",
    "shadow_concurrency": "3",
    "triage_concurrency": "2",
    "clone": lambda t: str(t.clone2),
    "base": "develop",
    "grace_min": "40",
    "ttl_min": "50",
    "inflight_ttl_min": "12",
    "turn_budget_s": "1200",
    "host": "https://other-gateway.example",
    "reviewer_profile": "scribe",
    "fixer_profile": "editor",
    "reviewer_login": "rev-other",
    "fixer_login": "fix-other",
    "adjudicator_profile": "tester",
    "reviewer_token_file": lambda t: str(t.pats["rev2"]),
    "fixer_token_file": lambda t: str(t.pats["fix2"]),
    "adjudicator_login": "adj-other",
    "adjudicator_token_file": lambda t: str(t.pats["adj2"]),
    "reviewer_app_id": "123",
    "reviewer_app_key_file": lambda t: str(t.pats["rev2"]),
    "fixer_app_id": "123",
    "fixer_app_key_file": lambda t: str(t.pats["fix2"]),
    "adjudicator_app_id": "123",
    "adjudicator_app_key_file": lambda t: str(t.pats["adj2"]),
    "reviewer_max_steps": "40",
    "fixer_max_steps": "41",
    "reviewer_daily_tokens": "100000",
    "shadow_daily_tokens": "200000",
    "fixer_daily_tokens": "300000",
    "auto_fix_labels": "bug, docs",
    "auto_fix_daily": "4",
    "fix_daily_turns": "6",
    "review_only": "someone",
    "operator_agents": "docs-agent, bug-hunter",
    "review_only_cap": "5",
    "observer_weekly_report": "tue 08:00",
    "ci_fix_cap": "2",
    "review_only_daily": "7",
    "review_only_update": True,
    "required_checks": "tests (3.11), lint",
    "shadow_profile": "arbiter",
    "shadow_every": "7",
    "shadow_escalate": "on",
    "human_paths": ".github/**, scripts/release*",
    "generated": "docs/commands.md=python3 tests/commands_doc.py --write",
    "fixer_check": "python3 tests/run_tests.py",
    "review_after_ci": True,
    "merge_check": "on",
    "ingress": "app-webhook",
    "ingress_app": "events-app[bot]",
    "fix_ci": True,
    "fixer_scope_lock": True,
    "attribution": False,
}

# Keys whose push needs the loop to already have a feature (triage, auto-fix labels).
NEEDS_AUTO_FIX_LABELS = {"auto_fix_daily"}


class ApplyEveryFormKey(unittest.TestCase):
    """Borrows the token-file fixture's temp home, stub GitHub and CLI runner."""
    setUp_fixture = tf.TokenFileSettingsTests.setUp
    pat = tf.TokenFileSettingsTests.pat
    run_cli = tf.TokenFileSettingsTests.run_cli
    init_argv = tf.TokenFileSettingsTests.init_argv
    loop_file = tf.TokenFileSettingsTests.loop_file
    loop_json = tf.TokenFileSettingsTests.loop_json

    def setUp(self):
        self.setUp_fixture()
        for profile in ("scribe", "editor", "tester"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        self.clone, self.clone2 = self.root / "clone", self.root / "clone2"
        self.clone.mkdir()
        self.clone2.mkdir()
        for name in ("fix2", "rev-other", "fix-other", "adj-other", "adj2"):
            self.pats[name] = self.pat(name)
        # A loop that has what each push needs: a clone (concurrency above 1 needs one), a second
        # trusted login per seat with its own token, an adjudicator, and a shadow reviewer.
        rc, out = self.run_cli(self.init_argv(
            "--clone", str(self.clone), "--reviewer", "rev-other", "--fixer", "fix-other",
            "--reviewer-seat", tf.REV, "--shadow-profile", "critic", "--shadow-every", "1",
            "--adjudicator-login", tf.ADJ, "--token", f"{tf.ADJ}={self.pats['adj']}",
            "--token", f"rev-other={self.pats['rev-other']}",
            "--token", f"fix-other={self.pats['fix-other']}",
            "--token", f"adj-other={self.pats['adj-other']}"))
        self.assertEqual(rc, 0, out)
        for route in ("widgets-triage", "widgets-observe"):
            self.addCleanup(lambda r=route: routes.restore_entries({r: None}))
        rc, out = self.run_cli(["triage", "--loop", "widgets", "--enable", "--triage-profile",
                                "arbiter", "--author", "Owner", "--labels", "bug,docs,P1",
                                "--fix-label", "agent-fix", "--maintainer", "owner"])
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--observer-profile", "arbiter",
                                "--observer-deliver", "telegram"])
        self.assertEqual(rc, 0, out)

    def value(self, key):
        push = PUSHES[key]
        return push(self) if callable(push) else push

    def test_every_schema_key_has_a_push(self):
        self.assertEqual(set(PUSHES), set(settings_form.SETTINGS_SCHEMA),
                         "a settings-form key needs a valid non-default value here, so apply's "
                         "'already matches' guard is proved to see it")

    def test_apply_never_says_already_matches_over_a_change_the_form_makes(self):
        self.every_key("routes installed", (0,))

    def test_the_same_with_no_route_installed_to_notice_the_change(self):
        # A moved host also drifts the installed routes' URLs, which would hide a missing host
        # comparison; with nothing installed, only the comparison itself can see the change.
        loop = config.load_id("widgets")
        routes.restore_entries({name: None for name in cli_common._routes_of(loop).values()})
        self.every_key("no routes installed", (0, 1))   # 1: the missing routes

    def every_key(self, state, exit_codes):
        for key in settings_form.SETTINGS_SCHEMA:
            with self.subTest(key=key, state=state), \
                    patch.object(cli_common, "_busy_seats", return_value=[]):
                if key in NEEDS_AUTO_FIX_LABELS:
                    rc, out = self.run_cli(["triage", "--loop", "widgets", "--auto-fix-label", "bug"])
                    self.assertEqual(rc, 0, out)
                settings = {key: self.value(key)}
                loop = config.load_id("widgets")
                if key in ("ingress", "ingress_app"):
                    # #1239: app-webhook needs a mapped App, and each key needs the other.
                    loop["apps"] = {**(loop.get("apps") or {}), "events-app[bot]": {
                        "app_id": 9, "key_file": str(self.pats["adj2"])}}
                    cli_common._write_config(loop)
                    settings = {"ingress": "app-webhook", "ingress_app": "events-app[bot]"}
                if "_app_" in key:
                    role = key.split("_", 1)[0]
                    login = f"test-{role}[bot]"
                    if role != "adjudicator":
                        allowlist = "reviewers" if role == "reviewer" else "fixers"
                        loop[allowlist] = [*loop[allowlist], login]
                        cli_common._write_config(loop)
                    settings.update({f"{role}_login": login,
                                     f"{role}_app_id": "123",
                                     f"{role}_app_key_file": self.value(f"{role}_app_key_file")})
                try:
                    pushed = config.normalize(settings_form.apply_settings(loop, settings))
                except config.ConfigError:
                    pushed = None       # refused before the comparison: nothing to compare
                # The row must really move the loop, or the assertion below proves nothing.
                self.assertIsNotNone(pushed, f"{key}: the push value is refused by apply_settings")
                self.assertNotEqual(pushed, loop, f"{key}: the push value changes nothing")
                rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"], settings)
                self.assertNotIn("already matches", out, f"{key}: {out}")
                # Never a refusal (2) — except a seat moving to a route nobody installed, which
                # refuses and so never says "already matches" either.
                if rc == 2 and "does not exist yet" in out and state != "routes installed":
                    continue
                # Moving to app-webhook installs the App's route, which a dry run reports as one
                # it would write (exit 1), whatever routes the loop's seats have (#1239).
                self.assertIn(rc, (*exit_codes, 1) if key in ("ingress", "ingress_app")
                              else exit_codes, f"{key}: {out}")
                # Past the guard: apply recognised a change and went on to say what it would write
                # (with no route installed the guard's early return prints no "matches" line at
                # all, only the routes' disagreement, so this is what shows the change was seen).
                self.assertIn("dry run — nothing written", out, f"{key}: {out}")


if __name__ == "__main__":
    unittest.main()
