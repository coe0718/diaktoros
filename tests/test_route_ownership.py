"""#1451: apply proves a route is the loop's by the intent record, not only by the prompt's wording.

A release that rewords a gate prompt leaves every installed route (and its record) on the old
wording. ``apply`` (``cli.common._verify_routes`` and ``cli.route_sync``'s repair filters) used to
refuse such a route as "not provably ours" — even the legacy-gate adjudicator route that doctor
names ``apply`` for. The rule is now ``route_intent.provably_ours``: this version's prompt, or the
record holds the route with the same gate, the configured profile and the same secret.
"""

from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diaktoros import config, doctor, gate_shims, prompts, route_intent, routes  # noqa: E402
from test_gate_shims import Base  # noqa: E402

OLD_REVIEWER = "the previous reviewer prompt"
OLD_ADJUDICATOR = "the previous adjudicator prompt"


class RecordedRouteOwnership(Base):
    def edit_registry(self, mutate):
        path = pathlib.Path(self.env["DIAKTOROS_SUBS"])
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2))

    def edit_config(self, mutate):
        path = config.config_dir() / "widgets.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def reworded(self, name, **fields):
        """An install from before a release changed ``name``: live route and record agree."""
        self.edit_registry(lambda data: data[name].update(**fields))
        route_intent.record_live(config.load_id("widgets"), [name])
        return routes.route(name)["secret"]

    def test_apply_rewrites_a_recorded_route_whose_prompt_predates_a_reword(self):
        self.full_install()
        secret = self.reworded("widgets-review", prompt=OLD_REVIEWER)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        live = routes.route("widgets-review")
        self.assertEqual(live["prompt"], prompts.REVIEWER)
        self.assertEqual(live["secret"], secret, "same secret: the repo hook still authenticates")
        loop = config.load_id("widgets")
        self.assertEqual(route_intent.load(loop)["widgets-review"]["prompt"], prompts.REVIEWER)

    def test_a_profile_change_is_not_refused_over_a_recorded_reworded_route(self):
        # The state right after the operator moves a seat's profile in the config: the live route
        # and its record both still carry the old profile (critic) and the old prompt. Doctor
        # names a command for it, and that command must work (#2052 F1): ``_verify_routes`` must
        # accept the record's route (same gate, profile and secret as the live entry).
        self.full_install()
        secret = self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="default"))
        loop = config.load_id("widgets")
        self.assertEqual(routes.route_profile(route_intent.load(loop)["widgets-review"]), "critic")
        checks = {c.name: c for c in doctor.check_loop(loop, offline=True)}
        named = checks["route:widgets-review"]
        self.assertEqual(named.status, doctor.MISMATCH)
        self.assertIn("hermes dk apply --loop widgets", named.fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertNotIn("ownership cannot be verified", out)
        self.assertEqual(rc, 0, out)
        live = routes.route("widgets-review")
        self.assertEqual((live["profile"], live["prompt"], live["secret"]),
                         ("default", prompts.REVIEWER, secret))

    def test_doctor_does_not_name_apply_for_a_moved_route_apply_refuses(self):
        # The same profile move, but the live route carries another secret than the record: apply
        # refuses it, so doctor must not name apply first; it names the repair (which restores
        # the recorded route), and after it apply works.
        self.full_install()
        secret = self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_registry(lambda data: data["widgets-review"].update(secret="someone-elses"))
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="default"))
        loop = config.load_id("widgets")
        fix = {c.name: c for c in doctor.check_loop(loop, offline=True)}["route:widgets-review"].fix
        self.assertNotIn("to rebind it to default", fix)
        self.assertIn("--repair", fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIn("widgets-review", out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        live = routes.route("widgets-review")
        self.assertEqual((live["profile"], live["prompt"], live["secret"]),
                         ("default", prompts.REVIEWER, secret))

    def test_doctor_names_recreate_not_repair_for_a_reworded_route_with_another_secret(self):
        # Gate and profile match the config and the record, only the prompt is stale and the live
        # secret is not the recorded one: the record does not vouch for it (maintainer, #1451),
        # so doctor's fix is not ``--repair`` (#2052 F2).
        self.full_install()
        self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_registry(lambda data: data["widgets-review"].update(secret="someone-elses"))
        loop = config.load_id("widgets")
        fix = gate_shims.divergence(loop, contract=True)["widgets-review"][1]
        self.assertNotIn("--repair", fix)
        self.assertIn("--recreate-routes", fix)

    def test_apply_refuses_a_reworded_route_the_record_holds_with_another_secret(self):
        # A route re-created under a recorded name is not adopted (maintainer, #1451).
        self.full_install()
        self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_registry(lambda data: data["widgets-review"].update(secret="someone-elses"))
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="default"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("ownership cannot be verified", out)
        live = routes.route("widgets-review")
        self.assertEqual((live["secret"], live["prompt"]), ("someone-elses", OLD_REVIEWER))
        fix = gate_shims.divergence(config.load_id("widgets"), contract=True)["widgets-review"][1]
        self.assertNotIn("(rebinds the route to the config)`", fix)

    def test_apply_refuses_an_unrecorded_reworded_route(self):
        self.full_install()
        loop = config.load_id("widgets")
        route_intent.forget(loop, ["widgets-review"])
        self.edit_registry(lambda data: data["widgets-review"].update(prompt=OLD_REVIEWER))
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="default"))
        loop = config.load_id("widgets")
        # Doctor's profile check must not name the apply that refuses it (#2052 F1).
        fix = {c.name: c for c in doctor.check_loop(loop, offline=True)}["route:widgets-review"].fix
        self.assertNotIn("to rebind it to default", fix)
        self.assertIn("--recreate-routes", fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("ownership cannot be verified", out)
        self.assertEqual(routes.route("widgets-review")["prompt"], OLD_REVIEWER)

    def test_doctor_names_apply_for_a_legacy_adjudicator_route_and_apply_rebinds_it(self):
        self.full_install()
        secret = self.reworded("widgets-breach", script="gate_reviewer.py", prompt=OLD_ADJUDICATOR)
        loop = config.load_id("widgets")
        self.assertEqual(route_intent.heal(loop), [], "the heal leaves a legacy gate alone")
        fix = gate_shims.divergence(loop, contract=True)["widgets-breach"][1]
        self.assertIn("hermes dk apply --loop widgets", fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        live = routes.route("widgets-breach")
        self.assertEqual((live["script"], live["prompt"], live["secret"]),
                         ("gate_adjudicator.py", prompts.ADJUDICATOR, secret))
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        self.assertEqual(checks["gateway-script:widgets-breach"].status, doctor.VERIFIED)

    def prompt_check(self, name):
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        check = checks[f"route:{name}"]
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("gate's prompt", check.detail)
        return check.detail

    def test_doctor_says_the_record_vouches_for_a_recorded_reworded_route(self):
        # #2062: the record holds the route (gate, profile and secret), so doctor must not say
        # nothing proves it is the plugin's; it says the record vouches for the route.
        self.full_install()
        self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.reworded("widgets-breach", prompt=OLD_ADJUDICATOR)
        for name in ("widgets-review", "widgets-breach"):
            with self.subTest(route=name):
                detail = self.prompt_check(name)
                self.assertIn("but the loop's route record vouches for the route as this plugin's",
                              detail)
                self.assertNotIn("nothing proves", detail)

    def test_doctor_says_nothing_proves_an_unrecorded_or_unvouched_reworded_route(self):
        self.full_install()
        self.reworded("widgets-review", prompt=OLD_REVIEWER)
        self.edit_registry(lambda data: data["widgets-review"].update(secret="someone-elses"))
        route_intent.forget(config.load_id("widgets"), ["widgets-breach"])
        self.edit_registry(lambda data: data["widgets-breach"].update(prompt=OLD_ADJUDICATOR))
        for name in ("widgets-review", "widgets-breach"):   # another secret; no record at all
            with self.subTest(route=name):
                detail = self.prompt_check(name)
                self.assertIn("and nothing proves the route is this plugin's", detail)
                self.assertNotIn("vouches", detail)

    def test_doctor_names_recreate_not_apply_for_an_unrecorded_legacy_route(self):
        self.full_install()
        loop = config.load_id("widgets")
        route_intent.forget(loop, ["widgets-breach"])
        self.edit_registry(lambda data: data["widgets-breach"].update(
            script="gate_reviewer.py", prompt=OLD_ADJUDICATOR))
        fix = gate_shims.divergence(loop, contract=True)["widgets-breach"][1]
        self.assertNotIn("(rebinds the route to the config)", fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(routes.route("widgets-breach")["script"], "gate_reviewer.py", out)


if __name__ == "__main__":
    unittest.main()
