"""Issue dependencies on the Desktop (#1301): one owner (``issue_deps``) for list, declare and
waive, called by both ``hermes dk depends`` and the confirmed ``depends``/``waive`` actions.

GitHub is faked per path by the ``test_issue_deps`` fixture; the loop state, the gate script and
the sweep are real. Nothing here reaches the network.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_issue_deps as tid  # noqa: E402
import test_issue_fix as tif  # noqa: E402
from diaktoros import desktop_api, fix_hold, issue_deps, one_shot_action as actions  # noqa: E402

DepsFixture = type("DepsFixture", (tid.Deps,),
                   {name: None for name in dir(tid.Deps) if name.startswith("test_")})
TOKEN = "b" * 64


def corrupt(st, text):
    """Replace the dependency record with ``text`` (the state directory may not exist yet)."""
    st.dir.mkdir(parents=True, exist_ok=True)
    (st.dir / "issue-deps.json").write_text(text)


class Owner(DepsFixture):
    """The rules live in ``issue_deps``; the CLI only prints what they answer."""

    def test_refusals_say_why_and_write_nothing(self):
        self.st.issue_dep_add(5, 6, {"by": "operator", "at": "x"})
        self.st.issue_dep_add(6, 12, {"by": "operator", "at": "x"})
        before = self.st.issue_deps()
        self.issues[7] = {**tif.issue(number=7), "pull_request": {}}
        for on, code, words in ((12, 1, "cannot depend on itself"),
                                (5, 1, "a cycle: #12 → #5 → #6 → #12"),
                                (7, 1, "is a pull request"),
                                (99, 2, "could not be read")):
            with self.subTest(on=on):
                self.assertEqual(issue_deps.declare_problem(self.loop, self.st, 12, on)[0], code)
                got, why, added = issue_deps.declare(self.loop, self.st, 12, on)
                self.assertEqual((got, added), (code, False))
                self.assertIn(words, why)
                self.assertEqual(self.st.issue_deps(), before)

    def test_declare_and_waive_read_back(self):
        self.assertEqual(issue_deps.declare(self.loop, self.st, 12, 5), (0, "", True))
        self.assertEqual(self.st.issue_deps()[12][5]["by"], issue_deps.BY_CLI)
        self.assertEqual(issue_deps.declare(self.loop, self.st, 12, 5), (0, "", False))
        rows = issue_deps.listing(self.loop, self.st)
        self.assertEqual([(r["issue"], r["on"], r["state"]) for r in rows], [(12, 5, "waiting")])
        self.assertEqual(rows[0]["why"], "#5 is still open")
        self.assertTrue(issue_deps.waive(self.st, 12, 5))
        self.assertFalse(issue_deps.waive(self.st, 12, 5))
        self.assertEqual(self.st.issue_deps(), {})

    def test_cli_calls_the_owner_and_prints_its_answer(self):
        with mock.patch.object(issue_deps, "declare", return_value=(1, "owner says no", False)) \
                as declare:
            rc, out = self.depends(12, on=5)
        declare.assert_called_once_with(self.loop, mock.ANY, 12, 5)
        self.assertEqual((rc, out), (1, "[widgets] refused: owner says no\n"))
        with mock.patch.object(issue_deps, "declare", return_value=(2, "#5 unreadable", False)):
            self.assertEqual(self.depends(12, on=5), (2, "[widgets] #5 unreadable\n"))
        with mock.patch.object(issue_deps, "waive", return_value=False) as waive:
            self.assertEqual(self.depends(12, waive=5)[0], 1)
        waive.assert_called_once_with(mock.ANY, 12, 5)

    def test_strict_read_refuses_an_unreadable_record(self):
        corrupt(self.st, "{not json")
        self.assertEqual(self.st.issue_deps(), {})               # the lenient reader is unchanged
        with self.assertRaises(ValueError):
            self.st.issue_deps(strict=True)
        corrupt(self.st, "[]")
        with self.assertRaises(ValueError):
            issue_deps.declared(self.st, strict=True)


class Confirmed(DepsFixture):
    """The confirmed ``depends``/``waive`` actions through ``one_shot_action``."""

    def plan(self, action, issue=12, on=5):
        return actions.plan(self.loop, action, pr=issue, on=on)

    def perform(self, action, confirmation, issue=12, on=5):
        return actions.confirmed_perform(self.loop, action, confirmation, pr=issue, on=on)

    def test_refused_previews_carry_no_confirmation_and_write_nothing(self):
        self.st.issue_dep_add(5, 6, {"by": "operator", "at": "x"})
        self.st.issue_dep_add(6, 12, {"by": "operator", "at": "x"})
        before = self.st.issue_deps()
        self.issues[7] = {**tif.issue(number=7), "pull_request": {}}
        for on, words in ((12, "cannot depend on itself"), (5, "a cycle: #12 → #5 → #6 → #12"),
                          (7, "is a pull request"), (99, "could not be read from GitHub")):
            with self.subTest(on=on):
                offer = self.plan("depends", on=on)
                self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
                self.assertIn(words, offer["reason"])
                self.assertEqual(self.st.issue_deps(), before)
        offer = self.plan("waive", on=9)
        self.assertFalse(offer["eligible"])
        self.assertIn("does not depend on #9", offer["reason"])
        self.assertEqual(self.posted, [])
        # A confirmation for a refused target is never honoured, whatever it is.
        self.assertEqual(self.perform("depends", TOKEN, on=12)["status"], "stale")
        self.assertEqual(self.st.issue_deps(), before)

    def test_declared_from_the_page_holds_the_hand_off_and_a_waiver_releases_it(self):
        offer = self.plan("depends")
        self.assertTrue(offer["eligible"])
        self.assertEqual((offer["pr"], offer["on"], offer["head"]), (12, 5, ""))
        self.assertIn("records #12 waits on #5", offer["reason"])
        public = actions.public_plan(offer)
        self.assertEqual(public["on"], 5)
        self.assertEqual(self.st.issue_deps(), {})               # a preview writes nothing
        result = self.perform("depends", offer["confirmation"])
        self.assertEqual((result["ok"], result["status"]), (True, "verified"), result)
        self.assertEqual(self.st.issue_deps()[12][5]["by"], issue_deps.BY_DESKTOP)
        self.run_gate(self.labeled())                            # held exactly as the CLI's is
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
        again = self.plan("depends")
        self.assertFalse(again["eligible"])
        self.assertIn("already depends on #5", again["reason"])
        waiver = self.plan("waive")
        self.assertTrue(waiver["eligible"], waiver)
        self.assertIn("removes #12's dependency on #5", waiver["reason"])
        result = self.perform("waive", waiver["confirmation"])
        self.assertEqual((result["ok"], result["status"]), (True, "verified"), result)
        self.assertEqual(self.st.issue_deps(), {})
        fix_hold.sweep(self.loop, self.st)                       # released by the next sweep
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_record_changed_after_the_preview_is_stale_and_nothing_is_written(self):
        offer = self.plan("depends")
        self.st.issue_dep_add(12, 6, {"by": "operator", "at": "x"})     # another declaration
        result = self.perform("depends", offer["confirmation"])
        self.assertEqual((result["ok"], result["status"]), (False, "stale"))
        self.assertNotIn(5, self.st.issue_deps()[12])
        self.st.issue_dep_add(12, 5, {"by": "operator", "at": "x"})
        waiver = self.plan("waive")
        self.st.issue_dep_drop(12, 6)                                    # another waiver
        result = self.perform("waive", waiver["confirmation"])
        self.assertEqual(result["status"], "stale")
        self.assertIn(5, self.st.issue_deps()[12])

    def test_confirmation_is_bound_to_the_exact_target_and_action(self):
        offer = self.plan("depends")
        for action, on in (("depends", 6), ("waive", 5)):
            with self.subTest(action=action, on=on):
                self.assertEqual(self.perform(action, offer["confirmation"], on=on)["status"],
                                 "stale")
        self.assertEqual(self.st.issue_deps(), {})

    def test_a_write_the_read_back_cannot_see_is_never_verified(self):
        offer = self.plan("depends")
        with mock.patch.object(issue_deps, "declare", return_value=(0, "", True)):
            result = self.perform("depends", offer["confirmation"])
        self.assertEqual((result["ok"], result["status"]), (False, "unknown"))
        offer = self.plan("depends")
        with mock.patch.object(issue_deps, "declare", side_effect=OSError("/private/state")):
            result = self.perform("depends", offer["confirmation"])
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("/private", json.dumps(result))

    def test_an_unreadable_record_offers_nothing(self):
        corrupt(self.st, "{not json")
        for action in ("depends", "waive"):
            with self.subTest(action=action):
                offer = self.plan(action)
                self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
                self.assertIn("unavailable", offer["reason"])

    def test_other_actions_take_no_second_issue(self):
        offer = actions.plan(self.loop, "escalate", pr=7, on=5)
        self.assertFalse(offer["eligible"])
        self.assertEqual(offer["reason"], "invalid action target")


class View(DepsFixture):
    """``deps_view``: the list, bounded and never a raw error."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(desktop_api, "_loop", return_value=self.loop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rows_match_the_cli_and_never_echo_transport_text(self):
        self.depends(12, on=5)
        self.depends(12, on=6)
        self.issues[13] = tif.issue(number=13)
        self.depends(13, on=5)
        self.issues[6] = {**self.issues[6], "state": "closed"}
        self.events[6] = None                              # unreadable: "could not be read in full"
        view = desktop_api.deps_view("widgets")
        self.assertEqual((view["loop"], view["repo"], view["available"], view["total"]),
                         ("widgets", tid.REPO, True, 3))
        self.assertEqual([(r["issue"], r["on"], r["state"]) for r in view["deps"]],
                         [(12, 5, "waiting"), (12, 6, "unknown"), (13, 5, "waiting")])
        self.assertEqual(view["deps"][0]["why"], "#5 is still open")
        self.assertEqual(view["deps"][0]["by"], "operator")
        self.assertRegex(view["deps"][0]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        cli = self.depends()[1]
        for line in ("#12 depends on #5: waiting — #5 is still open",
                     "#13 depends on #5: waiting — #5 is still open"):
            self.assertIn(line, cli)
        with mock.patch.object(issue_deps, "met", side_effect=[
                (None, "events page 1: GET https://api.github.com ghp_SECRET"),
                RuntimeError("/private/PAT\n" * 50)]):
            view = desktop_api.deps_view("widgets")
        text = json.dumps(view)
        for leak in ("ghp_", "https://", "/private", "RuntimeError"):
            self.assertNotIn(leak, text)
        self.assertEqual([r["state"] for r in view["deps"]], ["unknown"] * 3)
        self.assertTrue(all(len(r["why"]) <= 240 for r in view["deps"]))

    def test_rows_are_bounded_and_each_prerequisite_read_once(self):
        for issue in range(100, 160):
            self.st.issue_dep_add(issue, 5, {"by": "operator", "at": "2026-01-01T00:00:00Z"})
        with mock.patch.object(issue_deps, "met", return_value=(False, "#5 is still open")) as met:
            view = desktop_api.deps_view("widgets")
        self.assertEqual((view["total"], len(view["deps"])), (60, desktop_api.DEPS_MAX))
        self.assertEqual(met.call_count, 1)

    def test_an_unreadable_record_is_unavailable_not_none_declared(self):
        corrupt(self.st, "{not json")
        view = desktop_api.deps_view("widgets")
        self.assertEqual((view["available"], view["deps"], view["total"]), (False, [], 0))

    def test_a_verified_empty_list_is_available(self):
        view = desktop_api.deps_view("widgets")
        self.assertEqual((view["available"], view["deps"], view["total"]), (True, [], 0))

    def test_unsafe_who_and_when_are_withheld(self):
        self.st.issue_dep_add(12, 5, {"by": "/home/x/.hermes token", "at": "\n" * 5})
        with mock.patch.object(issue_deps, "met", return_value=(False, "#5 is still open")):
            row = desktop_api.deps_view("widgets")["deps"][0]
        self.assertEqual((row["by"], row["at"]), (None, None))

    def test_a_loop_that_does_not_load_is_not_found(self):
        with mock.patch.object(desktop_api, "_loop", side_effect=RuntimeError("/private")):
            with self.assertRaises(desktop_api.NotFound) as caught:
                desktop_api.deps_view("widgets")
        self.assertNotIn("/private", str(caught.exception))
        with self.assertRaises(desktop_api.BadRequest):
            desktop_api.deps_view("x" * 201)


LOOP = {"id": "one", "repo": "acme/widgets"}


class Boundary(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(desktop_api, "_loop", return_value=LOOP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_depends_and_waive_need_two_issue_numbers_and_no_seat(self):
        bad = [("depends", 12, None, None), ("depends", 12, None, 0), ("waive", True, None, 5),
               ("depends", 12, "reviewer", 5), ("waive", "12", None, 5), ("depends", 12, None, "5"),
               ("escalate", 7, None, 5), ("cancel", 7, None, 5)]
        with mock.patch.object(actions, "plan") as plan:
            for action, pr, seat, on in bad:
                with self.subTest(action=action, pr=pr, seat=seat, on=on), \
                        self.assertRaises(desktop_api.BadRequest):
                    desktop_api.action_view("one", action, pr, seat, on)
        plan.assert_not_called()

    def test_preview_and_perform_forward_on_to_the_shared_owner(self):
        offer = dict(action="depends", pr=12, seat=None, on=5, eligible=True, head="",
                     reason="records #12 waits on #5", confirmation=TOKEN, record="/private")
        with mock.patch.object(actions, "plan", return_value=offer) as plan:
            result = desktop_api.action_view("one", "depends", 12, None, 5)
        plan.assert_called_once_with(LOOP, "depends", pr=12, seat=None, on=5)
        self.assertEqual((result["on"], result["confirmation"]), (5, TOKEN))
        self.assertNotIn("/private", json.dumps(result))
        with mock.patch.object(actions, "confirmed_perform", return_value={
                "ok": True, "status": "verified", "reason": "#12 waits on #5", "head": ""}) as perform:
            result = desktop_api.action_perform_view("one", "waive", 12, None, TOKEN, 5)
        perform.assert_called_once_with(LOOP, "waive", TOKEN, pr=12, seat=None, on=5)
        self.assertTrue(result["ok"])
        for status in ("stale", "refused"):
            with self.subTest(status=status), mock.patch.object(actions, "confirmed_perform", return_value={
                    "ok": False, "status": status, "reason": "x", "head": ""}), \
                    self.assertRaises(desktop_api.Conflict):
                desktop_api.action_perform_view("one", "depends", 12, None, TOKEN, 5)


try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi = None


@unittest.skipUnless(fastapi, "FastAPI is not installed")
class Routes(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "dashboard/plugin_api.py"
        spec = importlib.util.spec_from_file_location("desktop_deps_route_test", path)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)

    def test_post_body_takes_on_only_for_depends_and_waive(self):
        good = dict(loop="one", action="depends", pr=12, seat=None, on=5, confirmation=TOKEN)
        five = {k: v for k, v in good.items() if k != "on"}
        with mock.patch.object(desktop_api, "action_perform_view", return_value={}) as perform:
            for body in (five, {**good, "token": "x"}, {**good, "action": "escalate"},
                         {**five, "action": "waive"}):
                with self.subTest(body=body), self.assertRaises(self.api.HTTPException) as caught:
                    self.api.actions_perform(body)
                self.assertEqual(caught.exception.status_code, 422)
            perform.assert_not_called()
            self.api.actions_perform(good)
            perform.assert_called_once_with("one", "depends", 12, None, TOKEN, 5)
            perform.reset_mock()
            self.api.actions_perform({**five, "action": "escalate", "pr": 7})
            perform.assert_called_once_with("one", "escalate", 7, None, TOKEN)

    def test_get_parses_on_and_keeps_the_old_call_without_it(self):
        with mock.patch.object(desktop_api, "action_view", return_value={}) as view:
            self.api.actions(loop="one", action="depends", pr="12", seat=None, on="5")
            view.assert_called_once_with("one", "depends", 12, None, 5)
            view.reset_mock()
            self.api.actions(loop="one", action="escalate", pr="7", seat=None, on=None)
            view.assert_called_once_with("one", "escalate", 7, None)
        for on in ("x", "-1", "5" * 13):
            with self.subTest(on=on), self.assertRaises(self.api.HTTPException) as caught:
                self.api.actions(loop="one", action="depends", pr="12", seat=None, on=on)
            self.assertEqual(caught.exception.status_code, 422)

    def test_deps_read_is_bounded_on_failure(self):
        with mock.patch.object(desktop_api, "deps_view",
                               side_effect=RuntimeError("/private/PAT ghp_x")):
            with self.assertRaises(self.api.HTTPException) as caught:
                self.api.deps(loop="one")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("/private", str(caught.exception.detail))
        with mock.patch.object(desktop_api, "deps_view", return_value={"ok": 1}) as view:
            self.assertEqual(self.api.deps(loop="one"), {"ok": 1})
        view.assert_called_once_with("one")


if __name__ == "__main__":
    unittest.main()
