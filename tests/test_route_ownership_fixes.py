"""The fix ``gate_shims.divergence`` and doctor name for a route on the wrong profile is never an
``apply`` that refuses it (#1451, #2052), in the corners #2058, #2069 and #2071 found.
"""

from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diaktoros import config, doctor, prompts, route_intent, routes  # noqa: E402
from diaktoros.doctor import route_checks  # noqa: E402
from diaktoros.gate import shims as gate_shims  # noqa: E402
from test_gate_shims import Base  # noqa: E402
from test_route_ownership import OLD_REVIEWER, RouteEdits  # noqa: E402

REBIND = "`hermes dk apply --loop widgets` (rebinds the route to the config)"
REPAIR = "`hermes dk doctor --loop widgets --repair` (restores the route the plugin wrote)"


class ProfileFixNamesNoRefusedApply(RouteEdits, Base):
    def fixes(self, name="widgets-review"):
        """(divergence's fix, doctor's fix) for route ``name``."""
        loop = config.load_id("widgets")
        checks = {c.name: c for c in doctor.check_loop(loop, offline=True)}
        return gate_shims.divergence(loop, contract=True)[name][1], checks[f"route:{name}"].fix

    def assert_apply_refuses(self):
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("ownership cannot be verified", out)

    def assert_apply_rebinds(self, secret):
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        live = routes.route("widgets-review")
        self.assertEqual((routes.route_profile(live), live["prompt"], live["secret"]),
                         (config.seat_profile(config.load_id("widgets"), "reviewer"),
                          prompts.REVIEWER, secret))

    def test_null_profile_unrecorded_stale_route_names_recreate_not_apply(self):
        # #2069: a null profile (the gateway refuses every request), a stale prompt and no record:
        # apply refuses the route, so neither divergence nor doctor may name it.
        self.full_install()
        route_intent.forget(config.load_id("widgets"), ["widgets-review"])
        self.edit_registry(lambda data: data["widgets-review"].update(
            prompt=OLD_REVIEWER, profile=None))
        for fix in self.fixes():
            with self.subTest(fix=fix):
                self.assertNotIn(REBIND, fix)
                self.assertIn("--recreate-routes", fix)
        self.assert_apply_refuses()

    def test_null_profile_recorded_stale_route_names_repair_then_apply(self):
        # #2069 and #2071: the record agrees with the live entry (null profile, stale prompt, same
        # secret), so the intent overlay stays out of the way and doctor's own profile-mismatch
        # choice is what is shown. Apply refuses the route (the record cannot vouch for a null
        # profile, and the prompt is stale), so the fix is the repair — which upgrades the
        # recorded prompt — then apply, and following it works.
        self.full_install()
        secret = self.reworded("widgets-review", prompt=OLD_REVIEWER, profile=None)
        want = f"{REPAIR}, then {REBIND}"
        divergence_fix, doctor_fix = self.fixes()
        self.assertEqual(divergence_fix, want)
        self.assertEqual(doctor_fix, want)
        self.assertNotIn("to rebind it to", doctor_fix)
        self.assert_apply_refuses()
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertEqual(routes.route("widgets-review")["prompt"], prompts.REVIEWER, out)
        self.assert_apply_rebinds(secret)

    def test_null_profile_stale_route_with_a_restorable_record_names_repair_alone(self):
        # Review on #2583: the record still holds the route at the configured profile (gate,
        # prompt, secret), and only the live entry went stale with a null profile. The repair
        # restores the whole route, so it is the one fix: not apply (refused), not repair then
        # apply, not recreate. Doctor's intent overlay names the same repair.
        self.full_install()
        secret = routes.route("widgets-review")["secret"]
        self.edit_registry(lambda data: data["widgets-review"].update(
            prompt=OLD_REVIEWER, profile=None))
        divergence_fix, doctor_fix = self.fixes()
        self.assertEqual(divergence_fix, REPAIR)
        self.assertEqual(doctor_fix, route_checks.REPAIR_FIX)
        self.assert_apply_refuses()
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        live = routes.route("widgets-review")
        self.assertEqual((routes.route_profile(live), live["prompt"], live["secret"]),
                         (config.seat_profile(config.load_id("widgets"), "reviewer"),
                          prompts.REVIEWER, secret), out)
        self.assertNotIn("widgets-review",
                         gate_shims.divergence(config.load_id("widgets"), contract=True))
        self.assert_apply_rebinds(secret)

    def test_owned_fix_names_no_repair_for_a_reworded_route_moved_twice(self):
        # #2058, revised by #1446: the record holds the route with its gate and secret but the
        # profile the config had before (critic, old prompt); since then the config moved to
        # default and something rebound the live entry to a third profile. Record, live route
        # and config name three profiles: the repair touches nothing there (it would move the
        # seat to critic, which the config does not name), so no fix may name it first.
        self.full_install()
        secret = self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_registry(lambda data: data["widgets-review"].update(profile="other"))
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="default"))
        loop = config.load_id("widgets")
        self.assertEqual(routes.route_profile(route_intent.load(loop)["widgets-review"]), "critic")
        divergence_fix, doctor_fix = self.fixes()
        for fix in (divergence_fix, doctor_fix):
            with self.subTest(fix=fix):
                self.assertNotIn(REPAIR, fix)
                self.assertNotIn("--repair", fix)
        self.assert_apply_refuses()
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIn(routes.PROFILE_CONFLICT, out)
        live = routes.route("widgets-review")
        self.assertEqual((live["profile"], live["prompt"], live["secret"]),
                         ("other", OLD_REVIEWER, secret), out)
        self.assertEqual(route_intent.load(loop)["widgets-review"]["profile"], "critic")


if __name__ == "__main__":
    unittest.main()
