"""#1446: the route heal weighs a drifted profile against the loop config, the intent record and
the live route, and never moves a seat to a profile the config does not name.

One test per row of the maintainer's table (issue #1446, 2026-10-30), driven through the
enforcing entry points: the watchdog's armed sweep (``watchdog.sweep_loop``) and the heal that
``doctor --repair`` runs (``route_intent.heal``), plus the reports that must agree with them
(``route_intent.drift`` for doctor's intent overlay). ``doctor --repair`` through the CLI is
covered for the conflict row in ``test_route_ownership_fixes``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diaktoros import doctor, observer, route_intent, routes, watch_mode  # noqa: E402
from diaktoros.doctor import route_checks  # noqa: E402
from scripts import watchdog  # noqa: E402
from test_route_self_heal import Fixture  # noqa: E402


class ProfileVerdictTable(unittest.TestCase):
    """``routes.profile_verdict``: the one rule, row by row."""

    def test_rows(self):
        p = lambda name: {"profile": name}  # noqa: E731
        cases = [
            ("rev", p("rev"), p("rev"), ""),
            ("rev", p("old"), p("rev"), "refresh"),       # row 1
            ("new", p("rev"), p("rev"), "apply"),         # row 2
            ("rev", p("rev"), p("other"), "restore"),     # row 3
            ("rev", p("rev"), None, "restore"),           # row 3, erased
            ("rev", p("old"), p("other"), "conflict"),    # row 4
            ("rev", p("old"), None, "conflict"),          # row 4, erased
            ("", p("old"), p("other"), "restore"),        # no configured profile: the record
        ]
        for configured, recorded, live, want in cases:
            with self.subTest(configured=configured, recorded=recorded, live=live):
                self.assertEqual(routes.profile_verdict(configured, recorded, live), want)


class ProfileHealRows(Fixture):
    def sweep(self):
        """One armed watchdog sweep (the heal runs on every one), with GitHub stubbed out."""
        st = mock.Mock()
        st.dir = pathlib.Path(self.tmp.name) / "state"
        st.watch.return_value = {}
        with mock.patch.object(watchdog.gate, "hooks_read", return_value=(True, "")), \
                mock.patch.object(watchdog.gh, "auth_probe",
                                  return_value=watchdog.gh.Response({"login": "rev"}, "", 200, {})), \
                mock.patch.object(watch_mode, "TEST", False), \
                mock.patch.object(watchdog.gh, "open_prs", return_value=None):
            return watchdog.sweep_loop(self.loop, st)

    def record_profile(self, name, profile):
        entry = route_intent.load(self.loop)[name]
        route_intent.record(self.loop, {name: dict(entry, profile=profile)})

    def recorded(self, name="widgets-review"):
        return route_intent.load(self.loop)[name]

    def test_row1_live_is_config_record_stale_refreshes_the_record_and_moves_nothing(self):
        before = self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d["widgets-review"].update(secret="rotated"))   # another drift
        self.assertEqual(route_intent.drift(self.loop, self.live(), route_intent.load(self.loop)),
                         {"widgets-review": ["secret", route_intent.STALE_PROFILE]})
        lines = self.sweep()
        live = self.live()["widgets-review"]
        self.assertEqual((live["profile"], live["secret"]),
                         ("rev", before["widgets-review"]["secret"]), lines)
        self.assertEqual(self.recorded()["profile"], "rev")
        self.assertTrue(any("was stale; refreshed to 'rev'" in line for line in lines), lines)
        self.assertTrue(any("widgets-review: had changed secret" in line for line in lines), lines)
        self.assertFalse(any("had changed secret, profile" in line for line in lines), lines)
        self.assertEqual(route_intent.heal(self.loop), [], "refreshed stays refreshed")

    def test_row2_config_changed_without_apply_is_reported_and_nothing_moves(self):
        self.install()
        self.loop["seats"]["reviewer"]["profile"] = "new-profile"
        for lines in (self.sweep(), route_intent.heal(self.loop)):
            self.assertEqual(self.live()["widgets-review"]["profile"], "rev")
            self.assertEqual(self.recorded()["profile"], "rev")
            notice = [line for line in lines if "widgets-review" in line]
            self.assertEqual(len(notice), 1, lines)
            self.assertIn("NOT moved", notice[0])
            self.assertIn("'new-profile'", notice[0])
            self.assertIn("hermes dk apply --loop widgets", notice[0])

    def test_row2_other_drift_is_still_restored_without_moving_the_profile(self):
        before = self.install()
        self.loop["seats"]["reviewer"]["profile"] = "new-profile"
        self.native(lambda d: d["widgets-review"].update(secret="rotated"))
        lines = route_intent.heal(self.loop)
        live = self.live()["widgets-review"]
        self.assertEqual((live["profile"], live["secret"]),
                         ("rev", before["widgets-review"]["secret"]), lines)
        self.assertTrue(any("hermes dk apply" in line for line in lines), lines)

    def test_row3_outside_writer_is_restored_through_the_sweep(self):
        before = self.install()
        self.native(lambda d: d["widgets-review"].update(profile="elsewhere"))
        lines = self.sweep()
        self.assertEqual(self.live()["widgets-review"], before["widgets-review"])
        self.assertTrue(any("widgets-review: had changed profile" in line for line in lines), lines)

    def test_row4_three_profiles_is_a_conflict_and_nothing_is_touched(self):
        self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d["widgets-review"].update(profile="other"))
        registry, record = self.subs.read_bytes(), route_intent.path(self.loop).read_bytes()
        self.assertEqual(route_intent.drift(self.loop, self.live(), route_intent.load(self.loop)),
                         {"widgets-review": [routes.PROFILE_CONFLICT]})
        lines = self.sweep()
        self.assertEqual(self.subs.read_bytes(), registry, lines)
        self.assertEqual(route_intent.path(self.loop).read_bytes(), record)
        conflict = [line for line in lines if routes.PROFILE_CONFLICT in line]
        self.assertEqual(len(conflict), 1, lines)
        for name in ("'rev'", "'old-profile'", "'other'", "NOT restored"):
            self.assertIn(name, conflict[0])
        self.assertFalse(any("restored 1 route" in line for line in lines), lines)

    def test_row4_an_erased_route_is_not_recreated_on_an_unconfigured_profile(self):
        self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d.pop("widgets-review"))
        registry = self.subs.read_bytes()
        self.assertEqual(route_intent.drift(self.loop, self.live(), route_intent.load(self.loop)),
                         {"widgets-review": ["missing", routes.PROFILE_CONFLICT]})
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.subs.read_bytes(), registry, lines)
        self.assertNotIn("widgets-review", self.live())
        self.assertTrue(any(routes.PROFILE_CONFLICT in line and "the route is missing" in line
                            for line in lines), lines)
        self.assertFalse(any("was missing" in line for line in lines), lines)

    def test_row4_doctor_does_not_name_the_repair(self):
        self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d["widgets-review"].update(profile="other"))
        checks = [doctor.Check("route:widgets-review", doctor.VERIFIED, "rev")]
        out = route_checks._intent_overlay(self.loop, self.live(), checks)[0]
        self.assertEqual(out.status, doctor.MISMATCH)
        self.assertIn(routes.PROFILE_CONFLICT, out.detail)
        self.assertNotIn("--repair", out.fix)
        self.assertEqual(out.fix, route_checks.PROFILE_CONFLICT_FIX)

    def test_a_foreign_entry_never_refreshes_the_record(self):
        # Review of #2756, F4: the live entry serves the config's profile but runs another
        # script. The heal reports it and must not rewrite the plugin's record from it either.
        self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d["widgets-review"].update(script="foreign.py"))
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.live()["widgets-review"]["script"], "foreign.py")
        self.assertEqual(self.recorded()["profile"], "old-profile")
        self.assertFalse(any("refreshed" in line for line in lines), lines)
        self.assertTrue(any("something else holds this name" in line for line in lines), lines)

    def test_heal_entries_never_offers_a_foreign_entry_for_refresh(self):
        # Review of #2756, round 2, F3: the candidate for a record refresh is chosen only after
        # the live entry is established as the plugin's (routes.heal_entries checks ``owned``
        # first). Selecting it before that check hands the caller a foreign entry to copy.
        self.install()
        self.record_profile("widgets-review", "old-profile")
        self.native(lambda d: d["widgets-review"].update(script="foreign.py"))
        expected = {"widgets-review": self.recorded()}
        restored, conflicts, refreshed = routes.heal_entries(
            expected, route_intent.watched("reviewer"), route_intent.owned,
            profiles=route_intent.configured_profiles(self.loop))
        self.assertEqual((restored, refreshed), ({}, {}))
        self.assertIn("something else holds this name", conflicts["widgets-review"])

    def test_row4_an_outdated_recorded_prompt_is_not_upgraded_either(self):
        # Review of #2756, round 2, F1: the prompt upgrade ran before the conflict decision and
        # rewrote the record of a route the heal then reported as "nothing was touched".
        self.install()
        entry = self.recorded()
        route_intent.record(self.loop, {"widgets-review": dict(
            entry, profile="old-profile", prompt="the previous reviewer prompt")})
        self.native(lambda d: d["widgets-review"].update(profile="other"))
        registry, record = self.subs.read_bytes(), route_intent.path(self.loop).read_bytes()
        for lines in (self.sweep(), route_intent.heal(self.loop)):
            self.assertEqual(self.subs.read_bytes(), registry, lines)
            self.assertEqual(route_intent.path(self.loop).read_bytes(), record, lines)
            self.assertTrue(any(routes.PROFILE_CONFLICT in line for line in lines), lines)
            self.assertFalse(any("upgraded" in line for line in lines), lines)


class UrgentRouteConflict(Fixture):
    """Review of #2756, round 2, F6: an observer's urgent route has no route check of its own, so
    doctor's intent overlay writes its check; a profile conflict there must not name the repair,
    which touches nothing."""

    def setUp(self):
        super().setUp()
        self.loop["observer"] = {"route": "widgets-feed", "urgent_route": "widgets-urgent",
                                 "profile": "default", "urgent_profile": "pager",
                                 "deliver": "telegram"}
        self.native(lambda d: d.update({
            "widgets-feed": {"secret": "f", **observer.route_contract(self.loop)},
            "widgets-urgent": {"secret": "u", **observer.route_contract(self.loop, True)}}))
        route_intent.record_live(self.loop, ["widgets-feed", "widgets-urgent"], replace=True)
        entry = route_intent.load(self.loop)["widgets-urgent"]
        route_intent.record(self.loop, {"widgets-urgent": dict(entry, profile="old-pager")})
        self.native(lambda d: d["widgets-urgent"].update(profile="other"))

    def test_doctor_names_the_conflict_and_the_heal_touches_nothing(self):
        checks = {check.name: check for check in route_checks.check_routes(self.loop)}
        check = checks["route:widgets-urgent"]
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn(routes.PROFILE_CONFLICT, check.detail)
        self.assertEqual(check.fix, route_checks.PROFILE_CONFLICT_FIX)
        self.assertNotIn("--repair", check.fix)
        registry = self.subs.read_bytes()
        lines = route_intent.heal(self.loop)
        self.assertEqual(self.subs.read_bytes(), registry, lines)
        self.assertTrue(any(routes.PROFILE_CONFLICT in line and "widgets-urgent" in line
                            for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
