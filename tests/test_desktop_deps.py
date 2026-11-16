"""Issue dependencies on the Desktop (#1301): one owner (``issue_deps``) for list, declare and
waive, called by both ``hermes dk depends`` and the confirmed ``depends``/``waive`` actions.

GitHub is faked per path by the ``test_issue_deps`` fixture; the loop state, the gate script and
the sweep are real. Nothing here reaches the network.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_issue_deps as tid  # noqa: E402
import test_issue_fix as tif  # noqa: E402
from diaktoros import (cli, config, desktop_api, explain as explain_mod, fix_hold,  # noqa: E402
                       issue_deps, ledger, one_shot_action as actions, run_supervisor,
                       state as state_mod)
from diaktoros.run_supervisor import Supervisor  # noqa: E402

DepsFixture = type("DepsFixture", (tid.Deps,),
                   {name: None for name in dir(tid.Deps) if name.startswith("test_")})
TOKEN = "b" * 64


def corrupt(st, text, name="issue-deps.json"):
    """Replace the dependency record (or ``name``) with ``text`` (the state directory may not
    exist yet)."""
    st.dir.mkdir(parents=True, exist_ok=True)
    (st.dir / name).write_text(text)


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


class CrossRepository(DepsFixture):
    """#1301: a dependency joins two issues of this repository. A number GitHub answers with
    another repository's issue (a transferred issue) is refused in the preview and by the CLI,
    with nothing written; a repository-qualified target never reaches the owner at all."""

    OTHER = "https://api.github.com/repos/acme/other"

    def setUp(self):
        super().setUp()
        self.issues[8] = {**tif.issue(number=8), "repository_url": self.OTHER}
        self.issues[9] = {**tif.issue(number=31), "repository_url": self.OTHER + "/"}

    def assert_nothing_written(self):
        self.assertEqual(self.st.issue_deps(), {})
        self.assertFalse((self.st.dir / "issue-deps.json").exists())
        self.assertEqual(self.posted, [])

    def test_an_issue_of_another_repository_is_refused_in_the_preview(self):
        for issue, on, named in ((12, 8, 8), (12, 9, 9), (8, 5, 8)):
            with self.subTest(issue=issue, on=on):
                offer = actions.plan(self.loop, "depends", pr=issue, on=on)
                self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
                self.assertEqual(offer["reason"], f"#{named} is an issue of another repository; "
                                                  "a dependency joins two issues of this repository")
                result = actions.confirmed_perform(self.loop, "depends", TOKEN, pr=issue, on=on)
                self.assertEqual((result["ok"], result["status"]), (False, "stale"))
                self.assertEqual(self.depends(issue, on=on),
                                 (1, f"[widgets] refused: #{named} is an issue of another "
                                     "repository; a dependency joins two issues of this repository\n"))
                self.assert_nothing_written()

    def test_this_repository_named_in_any_case_is_accepted(self):
        self.issues[10] = {**tif.issue(number=10),
                           "repository_url": "https://api.github.com/repos/" + tid.REPO.upper()}
        self.assertTrue(actions.plan(self.loop, "depends", pr=12, on=10)["eligible"])
        self.assertEqual(issue_deps.declare(self.loop, self.st, 12, 10), (0, "", True))

    def test_a_transferred_prerequisite_is_never_read_as_met(self):
        self.st.issue_dep_add(12, 8, {"by": "operator", "at": "x"})
        self.issues[8] = {**self.issues[8], "state": "closed"}
        self.assertEqual(issue_deps.blocking(self.loop, self.st, 12), [(8, "#8 could not be read")])

    def test_a_transferred_prerequisite_with_a_merged_ledger_fix_is_never_met(self):
        # #2399 review F8: the ledger's merged fix PR used to count before the issue was read.
        self.st.issue_dep_add(12, 8, {"by": "operator", "at": "x"})
        self.fix_prs[8] = [40]
        self.pulls[40] = {"number": 40, "merged_at": "2026-10-14T00:00:00Z", "base": {"ref": "main"}}
        self.assertEqual(issue_deps.met(self.loop, 8), (None, "#8 could not be read"))
        self.assertEqual(issue_deps.blocking(self.loop, self.st, 12), [(8, "#8 could not be read")])
        self.issues[8] = {**tif.issue(number=8)}                  # control: this repository's
        self.assertEqual(issue_deps.met(self.loop, 8), (True, "#8's fix PR #40 merged into main"))
        self.assertEqual(issue_deps.blocking(self.loop, self.st, 12), [])

    def test_a_repository_qualified_target_never_reaches_the_owner(self):
        from diaktoros.cli import operations
        parser = argparse.ArgumentParser()
        operations.add_depends(parser.add_subparsers())
        with mock.patch.object(issue_deps, "declare") as declare, \
                mock.patch.object(actions, "plan") as plan, \
                mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            for target in ("acme/other#5", "other#5", "#5"):
                with self.subTest(target=target):
                    with contextlib.redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as caught:
                        parser.parse_args(["depends", "--issue", "12", "--on", target])
                    self.assertEqual(caught.exception.code, 2)
                    with self.assertRaises(desktop_api.BadRequest):
                        desktop_api.action_view("widgets", "depends", 12, None, target)
        declare.assert_not_called()
        plan.assert_not_called()
        self.assert_nothing_written()


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


class FailClosed(DepsFixture):
    """#2150: an unreadable dependency record holds every issue; it is never "none declared"."""

    def test_a_corrupt_record_keeps_a_held_issue_held_until_it_can_be_read(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
        said = len(self.posted)
        good = (self.st.dir / "issue-deps.json").read_text()
        self.close_a(tid.MERGE)                                  # met: would release if read
        corrupt(self.st, "{not json")
        for _ in range(2):
            fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
        self.assertEqual(len(self.posted), said)                  # a local fault: nothing posted
        self.assertEqual(issue_deps.blocking(self.loop, self.st, 12),
                         [(None, issue_deps.UNREADABLE)])
        why = issue_deps.hold_reason(self.loop, 12)
        self.assertEqual(why, "waiting for " + issue_deps.UNREADABLE)
        self.assertNotIn(str(self.st.dir), why)
        self.assertIn("issue #12 fix waits for prerequisite its unreadable dependency record",
                      "\n".join(fix_hold.status_lines(self.loop, self.st)))
        (self.st.dir / "issue-deps.json").write_text(good)       # readable again: released
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_a_corrupt_record_holds_a_fresh_hand_off_and_a_merged_pr_wait(self):
        corrupt(self.st, "[1, 2")
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, self.st.dep_waits(), self.posted), ([], [12], []))
        self.st.dep_wait_drop(12)
        self.st.fix_hold_set(12, 30)                              # waits on a PR that merged
        with mock.patch.object(fix_hold, "pr_state", return_value="merged"):
            fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.fix_holds(), self.st.dep_waits()), ([], {}, [12]))

    def test_cli_on_a_corrupt_record_says_so_and_exits_2_without_a_traceback(self):
        corrupt(self.st, "{not json")
        for kwargs in ({}, {"issue": 12}, {"issue": 12, "on": 5}, {"issue": 12, "waive": 5}):
            with self.subTest(**kwargs):
                rc, out = self.depends(**kwargs)
                self.assertEqual(rc, 2, out)
                self.assertIn("the issue dependency record could not be read", out)
                self.assertNotIn("no issue dependencies declared", out)
                self.assertNotIn("Traceback", out)
                self.assertNotIn(str(self.st.dir), out)
        # Nothing was written over the record it could not read.
        self.assertEqual((self.st.dir / "issue-deps.json").read_text(), "{not json")

    # Valid JSON, malformed inside (#2399 review F1): each must read as unreadable, never as a
    # record with that part left out.
    MALFORMED = ('{"12": {"5": null}}', '{"12": null}', '{"12": [5]}', '{"x": {"5": {}}}',
                 '{"12": {"0": {}}}', '{"012": {"5": {}}}', '{"12": {"5": {"by": 3, "at": "x"}}}')

    def test_a_malformed_nested_record_keeps_a_held_issue_held(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
        self.close_a(tid.MERGE)                                  # met: would release if read
        for text in self.MALFORMED:
            with self.subTest(record=text):
                corrupt(self.st, text)
                with self.assertRaises(ValueError):
                    self.st.issue_deps(strict=True)
                fix_hold.sweep(self.loop, self.st)
                self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
                self.assertEqual(issue_deps.blocking(self.loop, self.st, 12),
                                 [(None, issue_deps.UNREADABLE)])

    def test_add_and_drop_on_a_malformed_nested_record_raise_and_write_nothing(self):
        for text in self.MALFORMED:
            with self.subTest(record=text):
                corrupt(self.st, text)
                with self.assertRaises(ValueError):
                    self.st.issue_dep_add(13, 6, {"by": "operator", "at": "x"})
                with self.assertRaises(ValueError):
                    self.st.issue_dep_drop(12, 5)
                for kwargs in ({}, {"issue": 12, "on": 6}, {"issue": 12, "waive": 5}):
                    rc, out = self.depends(**kwargs)
                    self.assertEqual(rc, 2, out)
                    self.assertIn("the issue dependency record could not be read", out)
                self.assertEqual((self.st.dir / "issue-deps.json").read_text(), text)
                self.assertFalse((self.st.dir / "issue-deps-waivers.json").exists())

    def test_a_dangling_symlink_record_holds_and_is_never_overwritten(self):
        # #2399 review F7: a dangling issue-deps.json (or waiver history) exists but cannot be
        # read; Path.exists() follows the link and says False, which read it as no record.
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.close_a(tid.MERGE)                                  # met: would release if read
        for name in ("issue-deps.json", "issue-deps-waivers.json"):
            with self.subTest(record=name):
                path = self.st.dir / name
                kept = path.read_text() if path.exists() else None
                path.unlink(missing_ok=True)
                path.symlink_to(self.st.dir / "gone" / name)
                with self.assertRaises(OSError):
                    (self.st.issue_deps if name == "issue-deps.json"
                     else self.st.issue_dep_waivers)(strict=True)
                with self.assertRaises(OSError):
                    self.st.issue_dep_drop(12, 5, {"by": "operator", "at": "x"})
                if name == "issue-deps.json":
                    with self.assertRaises(OSError):
                        self.st.issue_dep_add(13, 6, {"by": "operator", "at": "x"})
                    fix_hold.sweep(self.loop, self.st)
                    self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
                    rc, out = self.depends(issue=12, on=6)
                    self.assertEqual(rc, 2, out)
                self.assertTrue(path.is_symlink())                  # never replaced
                path.unlink()
                if kept is not None:
                    path.write_text(kept)

    def test_a_malformed_waiver_history_is_unreadable_not_none(self):
        self.depends(12, on=5)
        corrupt(self.st, '{"12": {"6": null}}', "issue-deps-waivers.json")
        rc, out = self.depends()
        self.assertEqual(rc, 0)
        self.assertIn("waiver history could not be read", out)


class Waivers(DepsFixture):
    """#1301: a waiver removes the dependency and keeps who waived it and when; it never blocks."""

    def test_a_cli_waiver_is_recorded_listed_and_releases_the_hold(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.assertEqual(self.st.dep_waits(), [12])
        self.assertEqual(self.depends(12, waive=5)[0], 0)
        self.assertEqual(self.st.issue_deps(), {})
        record = self.st.issue_dep_waivers()[12][5]
        self.assertEqual(record["by"], issue_deps.BY_CLI)
        self.assertRegex(record["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        rc, out = self.depends()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "[widgets] no issue dependencies declared\n"
                              f"[widgets] #12 no longer waits on #5 — waived by operator at "
                              f"{record['at']}\n")
        self.assertEqual(self.depends(13)[1], "[widgets] no issue dependencies declared\n")
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_no_reader_that_decides_a_hold_reads_the_waivers(self):
        self.depends(12, on=5)
        self.depends(12, on=6)
        self.depends(12, waive=6)
        corrupt(self.st, "{not json", "issue-deps-waivers.json")    # unreadable history
        with mock.patch.object(state_mod.LoopState, "issue_dep_waivers",
                               side_effect=AssertionError("a hold read the waivers")):
            self.run_gate(self.labeled())
            fix_hold.sweep(self.loop, self.st)
            self.assertEqual([a for a, _ in issue_deps.blocking(self.loop, self.st, 12)], [5])
            self.assertIn("#5", issue_deps.hold_reason(self.loop, 12))
            self.depends(12, waive=5)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
        # The waive above was refused (its history is unreadable): #5 still holds #12.
        self.assertIn(5, self.st.issue_deps()[12])
        rc, out = self.depends()
        self.assertEqual(rc, 0)
        self.assertIn("#12 depends on #5: waiting", out)
        self.assertIn("waiver history could not be read", out)
        corrupt(self.st, "{}", "issue-deps-waivers.json")
        self.assertEqual(self.depends(12, waive=5)[0], 0)
        self.assertEqual(issue_deps.blocking(self.loop, self.st, 12), [])
        self.assertEqual(issue_deps.hold_reason(self.loop, 12), "")
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_declaring_again_after_a_waiver_holds_again_and_clears_it(self):
        self.depends(12, on=5)
        self.depends(12, waive=5)
        self.assertIn(12, self.st.issue_dep_waivers())
        rc, out = self.depends(12, on=5)
        self.assertEqual(rc, 0)
        self.assertIn("now depends on #5", out)
        self.assertEqual(self.st.issue_dep_waivers(), {})
        self.assertEqual([a for a, _ in issue_deps.blocking(self.loop, self.st, 12)], [5])
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))

    def test_a_state_written_before_waivers_were_kept_reads_fine(self):
        self.st.dir.mkdir(parents=True, exist_ok=True)
        (self.st.dir / "issue-deps.json").write_text(
            json.dumps({"12": {"5": {"by": "operator", "at": "2026-01-01T00:00:00Z"}}}))
        self.assertFalse((self.st.dir / "issue-deps-waivers.json").exists())
        self.assertEqual((self.st.issue_dep_waivers(strict=True), issue_deps.waived(self.st)),
                         ({}, []))
        rc, out = self.depends()
        self.assertEqual(out, "[widgets] #12 depends on #5: waiting — #5 is still open\n")
        with mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            view = desktop_api.deps_view("widgets")
        self.assertEqual((view["available"], view["total"], view["waivers_available"],
                          view["waivers_total"], view["waivers"]), (True, 1, True, 0, []))
        self.assertTrue(issue_deps.waive(self.st, 12, 5))
        self.assertEqual(list(self.st.issue_dep_waivers()), [12])

    def test_a_page_waiver_is_recorded_as_desktop_and_read_back(self):
        self.depends(12, on=5)
        offer = actions.plan(self.loop, "waive", pr=12, on=5)
        self.assertIn("records the waiver as desktop", offer["reason"])
        result = actions.confirmed_perform(self.loop, "waive", offer["confirmation"], pr=12, on=5)
        self.assertEqual((result["ok"], result["status"]), (True, "verified"), result)
        self.assertEqual(self.st.issue_dep_waivers()[12][5]["by"], issue_deps.BY_DESKTOP)
        with mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            view = desktop_api.deps_view("widgets")
        self.assertEqual([(w["issue"], w["on"], w["by"]) for w in view["waivers"]],
                         [(12, 5, "desktop")])
        self.assertIn("waived by desktop at " + view["waivers"][0]["at"], self.depends()[1])

    def test_an_unreadable_waiver_history_offers_no_waiver(self):
        self.depends(12, on=5)
        corrupt(self.st, "{not json", "issue-deps-waivers.json")
        offer = actions.plan(self.loop, "waive", pr=12, on=5)
        self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
        self.assertIn("unavailable", offer["reason"])
        self.assertTrue(actions.plan(self.loop, "depends", pr=12, on=6)["eligible"])


def failing(name):
    """Every strict save of the state file ``name`` raises ``OSError`` (a full disk, say); the
    others land."""
    real = state_mod.LoopState._save_strict

    def save(self, path, data):
        if path.name == name:
            raise OSError("/private/state: No space left on device")
        return real(self, path, data)
    return mock.patch.object(state_mod.LoopState, "_save_strict", save)


class WaiverWrites(DepsFixture):
    """#2399 review F2: a waiver is reported only once its removal is durable, and never shown
    beside a dependency that still holds."""

    def waivers_on_page(self):
        with mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            return desktop_api.deps_view("widgets")["waivers"]

    def test_a_removal_that_fails_still_waits_and_shows_no_waiver(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        with failing("issue-deps.json"):
            rc, out = self.depends(12, waive=5)
        self.assertEqual(rc, 2, out)
        self.assertIn("could not be read or written — the dependency was not changed", out)
        self.assertNotIn("/private", out)
        rc, out = self.depends()
        self.assertEqual(out, "[widgets] #12 depends on #5: waiting — #5 is still open\n")
        # The history is written before the removal (#2399 review F6), so it may hold this
        # attempt; a waiver of a declared dependency is never shown, by any reader.
        self.assertEqual((issue_deps.waived(self.st, strict=True), self.waivers_on_page()), ([], []))
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))

    def test_a_history_write_that_fails_reports_the_waiver_as_unrecorded(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        with failing("issue-deps-waivers.json"):
            rc, out = self.depends(12, waive=5)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "[widgets] #12 no longer depends on #5; the next watchdog sweep "
                              "releases it if nothing else holds it; the waiver could not be "
                              "recorded in its history\n")
        self.assertEqual(self.st.issue_deps(), {})
        self.assertEqual(self.depends(), (0, "[widgets] no issue dependencies declared\n"))
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_a_page_waiver_whose_history_write_fails_says_so(self):
        self.depends(12, on=5)
        offer = actions.plan(self.loop, "waive", pr=12, on=5)
        with failing("issue-deps-waivers.json"):
            result = actions.confirmed_perform(self.loop, "waive", offer["confirmation"], pr=12, on=5)
        self.assertEqual((result["ok"], result["status"]), (True, "verified"), result)
        self.assertIn("the waiver could not be recorded in its history", result["reason"])
        self.assertEqual((self.st.issue_deps(), self.waivers_on_page()), ({}, []))

    def test_declared_again_when_the_old_waiver_cannot_be_cleared_it_only_waits(self):
        self.depends(12, on=5)
        self.depends(12, waive=5)
        with failing("issue-deps-waivers.json"):
            self.assertEqual(self.depends(12, on=5)[0], 0)
        self.assertIn(5, self.st.issue_dep_waivers()[12])        # the stale history is still there
        rc, out = self.depends()
        self.assertEqual(out, "[widgets] #12 depends on #5: waiting — #5 is still open\n")
        self.assertEqual(self.waivers_on_page(), [])


class WaiverView(DepsFixture):
    """``deps_view``'s waivers: bounded, withheld like ``by``/``at``, and never "none" unread."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(desktop_api, "_loop", return_value=self.loop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waivers_are_bounded_and_unsafe_who_and_when_withheld(self):
        corrupt(self.st, json.dumps({str(b): {"5": {"by": "/home/x/.hermes token", "at": "\n"}}
                                     for b in range(100, 160)}), "issue-deps-waivers.json")
        view = desktop_api.deps_view("widgets")
        self.assertEqual((view["waivers_available"], view["waivers_total"], len(view["waivers"])),
                         (True, 60, desktop_api.DEPS_MAX))
        self.assertEqual(view["waivers"][0], {"issue": 100, "on": 5, "by": None, "at": None})
        self.assertNotIn("/home", json.dumps(view))

    def test_unreadable_waivers_are_unavailable_and_leave_the_dependencies_readable(self):
        self.st.issue_dep_add(12, 5, {"by": "operator", "at": "2026-01-01T00:00:00Z"})
        corrupt(self.st, "[]", "issue-deps-waivers.json")
        with mock.patch.object(issue_deps, "met", return_value=(False, "#5 is still open")):
            view = desktop_api.deps_view("widgets")
        self.assertEqual((view["available"], view["total"]), (True, 1))
        self.assertEqual((view["waivers_available"], view["waivers_total"], view["waivers"]),
                         (False, 0, []))
        corrupt(self.st, "{not json")
        view = desktop_api.deps_view("widgets")
        self.assertEqual((view["available"], view["deps"], view["total"]), (False, [], 0))


class Explain(DepsFixture):
    """#1301: ``hermes dk explain`` reports an issue's dependencies and waivers exactly as
    ``hermes dk depends --issue N`` lists them (and the page reads them)."""

    REPORT = {"url": "u", "read_at": "t", "state_line": "s", "chain": {"status": "direct"},
              "budget": "b", "seat": "x", "queue": "q", "inflight": "i", "escalation": "e",
              "hooks": "h", "sweep": "w", "github": "g", "blockers": [], "next": {"action": "n"},
              "head": "a" * 40}

    def explain(self, number):
        """The ``depends:`` lines ``explain`` prints for ``number``."""
        out = io.StringIO()
        with mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(explain_mod, "explain_facts", return_value={}), \
                mock.patch.object(explain_mod, "explain", return_value=self.REPORT), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_explain(argparse.Namespace(loop="widgets", pr=number)), 0)
        return [line.strip()[len("depends:"):].strip() for line in out.getvalue().splitlines()
                if line.strip().startswith("depends:")]

    def listed(self, issue):
        """What ``hermes dk depends --issue N`` lists, without its prefix or "none declared"."""
        return [line.removeprefix("[widgets] ") for line in self.depends(issue)[1].splitlines()
                if not line.endswith("no issue dependencies declared")]

    def test_explain_reports_the_dependencies_and_waivers_the_cli_and_page_do(self):
        self.depends(12, on=5)
        self.depends(12, on=6)
        self.depends(12, waive=6)
        self.depends(13, on=5)
        lines = self.explain(12)
        self.assertEqual(lines, self.listed(12))
        self.assertEqual(lines[0], "#12 depends on #5: waiting — #5 is still open")
        self.assertRegex(lines[1], r"^#12 no longer waits on #6 — waived by operator at \d{4}-")
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.explain(13), self.listed(13))
        self.assertEqual(self.explain(40), [])                    # none declared: nothing said
        with mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            view = desktop_api.deps_view("widgets")
        page = [f"#{r['issue']} depends on #{r['on']}: {r['state']} — {r['why']}"
                for r in view["deps"] if r["issue"] == 12]
        page += [f"#{w['issue']} no longer waits on #{w['on']} — waived by {w['by']} at {w['at']}"
                 for w in view["waivers"] if w["issue"] == 12]
        self.assertEqual(page, lines)

    def test_explain_never_reads_an_unreadable_record_as_none(self):
        self.depends(12, on=5)
        corrupt(self.st, "{not json", "issue-deps-waivers.json")
        self.assertEqual(self.explain(12), ["#12 depends on #5: waiting — #5 is still open",
                                            issue_deps.WAIVERS_UNREADABLE])
        self.assertEqual(self.explain(12), self.listed(12))
        corrupt(self.st, '{"12": {"5": null}}')
        self.assertEqual(self.explain(12), [issue_deps.NOT_LISTED])
        self.assertEqual(self.depends(12), (2, f"[widgets] {issue_deps.NOT_LISTED}\n"))

    def test_explain_never_echoes_what_github_said_when_it_failed(self):
        self.depends(12, on=5)
        with mock.patch.object(issue_deps, "met", side_effect=RuntimeError("/private/PAT ghp_x")):
            lines = self.explain(12)
        self.assertEqual(lines, ["#12 depends on #5: unknown — #5 could not be read"])



class Crash(BaseException):
    """A process dying mid-call: no ``except Exception`` handler sees it."""


def crash_after(*names):
    """The process dies right after the first strict save of any of the state files ``names``
    lands: that write is durable, nothing after it in the call runs."""
    real = state_mod.LoopState._save_strict

    def save(self, path, data):
        real(self, path, data)
        if path.name in names:
            raise Crash(path.name)
    return mock.patch.object(state_mod.LoopState, "_save_strict", save)


class StaleWaiver(DepsFixture):
    """#2399 review F6: a waiver a redeclaration could not clear is never shown as a later
    waive's, whether that waive's history write fails or the process dies between its writes."""

    OLD = "2026-01-01T00:00:00Z"
    REPORT = Explain.REPORT
    WAITING = ["#12 depends on #5: waiting — #5 is still open"]

    def setUp(self):
        super().setUp()
        self.depends(12, on=5)
        with mock.patch("diaktoros.util.now_iso", return_value=self.OLD):
            self.depends(12, waive=5)                                 # waived by operator
        with failing("issue-deps-waivers.json"):                      # the cleanup fails
            self.assertEqual(self.depends(12, on=5)[0], 0)
        self.assertEqual(self.st.issue_dep_waivers()[12][5], {"by": "operator", "at": self.OLD})
        self.run_gate(self.labeled())
        self.offer = actions.plan(self.loop, "waive", pr=12, on=5)

    def page_waive(self):
        return actions.confirmed_perform(self.loop, "waive", self.offer["confirmation"],
                                         pr=12, on=5)

    def shown(self):
        """``(depends, explain, page)`` for #12, each as the sentences ``depends`` prints."""
        with mock.patch.object(desktop_api, "_loop", return_value=self.loop):
            view = desktop_api.deps_view("widgets")
        page = [f"#{r['issue']} depends on #{r['on']}: {r['state']} — {r['why']}"
                for r in view["deps"]]
        page += [f"#{w['issue']} no longer waits on #{w['on']} — waived by {w['by']} at {w['at']}"
                 for w in view["waivers"]]
        return Explain.listed(self, 12), Explain.explain(self, 12), page

    def assert_waits_and_shows_no_waiver(self):
        self.assertEqual(self.shown(), (self.WAITING,) * 3)
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))

    def assert_waived_by(self, by):
        lines = self.shown()
        self.assertEqual(lines[0], lines[1])
        self.assertEqual(lines[0], lines[2])
        self.assertEqual(len(lines[0]), 1, lines)
        self.assertRegex(lines[0][0], rf"^#12 no longer waits on #5 — waived by {by} at \d{{4}}-")
        self.assertNotIn(self.OLD, lines[0][0])
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, tid.BASE, "issue-fix")])

    def test_a_history_write_that_fails_over_the_old_waiver_removes_nothing(self):
        with failing("issue-deps-waivers.json"):
            result = self.page_waive()
            rc, out = self.depends(12, waive=5)
        self.assertEqual((result["ok"], result["status"]), (False, "unknown"), result)
        self.assertEqual(rc, 2, out)
        self.assertIn("the dependency was not changed", out)
        self.assertNotIn("/private", out + json.dumps(result))
        self.assert_waits_and_shows_no_waiver()
        self.assertEqual(self.depends(12, waive=5)[0], 0)              # writable again: done
        self.assert_waived_by("operator")

    def test_a_crash_after_the_removal_lands_never_shows_the_old_waiver(self):
        with crash_after("issue-deps.json"), self.assertRaises(Crash):
            self.page_waive()
        self.assert_waived_by("desktop")

    def test_a_crash_between_the_two_writes_never_shows_the_old_waiver(self):
        with crash_after("issue-deps.json", "issue-deps-waivers.json"), self.assertRaises(Crash):
            self.page_waive()
        self.assert_waits_and_shows_no_waiver()
        offer = actions.plan(self.loop, "waive", pr=12, on=5)            # the waive again: done
        result = actions.confirmed_perform(self.loop, "waive", offer["confirmation"], pr=12, on=5)
        self.assertEqual((result["ok"], result["status"]), (True, "verified"), result)
        self.assert_waived_by("desktop")

class DrawerIssue(unittest.TestCase):
    """``pr_view``'s ``issue`` (#1301): the issue whose dependencies the drawer shows, from the
    ledger only, and absent (unknown) when the ledger cannot say."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=__import__("os").environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.loops = [{"id": "widgets", "repo": tid.REPO, "cap": 3,
                       "state_dir": str(pathlib.Path(temp.name) / "state")}]
        self.n = 0

    def view(self, pr):
        with mock.patch.object(config, "readable_loops", return_value=(self.loops, [])), \
                mock.patch.object(run_supervisor, "production_ledger", return_value=self.db):
            return desktop_api.pr_view("widgets", pr)

    def run_row(self, pr, seat):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", tid.REPO, pr, chr(ord("a") + self.n) * 40, seat)
        with ledger.connect(self.db) as con:
            return con.execute("SELECT id FROM runs WHERE delivery=?", (f"d{self.n}",)).fetchone()[0]

    def test_an_issue_and_its_fix_pr_name_the_issue_and_a_plain_pr_names_none(self):
        run_id = self.run_row(12, "issue_fixer")
        self.run_row(40, "reviewer")
        self.run_row(41, "reviewer")
        now = time.time()
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO issue_fixes(run_id,repo,number,base,kind,branch,state,"
                        "pr_number,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, tid.REPO, 12, "main", "pr", "fix/12", "pushed", 40, now, now))
        self.assertEqual(self.view(12)["issue"], 12)
        self.assertEqual(self.view(40)["issue"], 12)
        self.assertIsNone(self.view(41)["issue"])

    def test_no_ledger_or_an_older_one_leaves_the_issue_unknown(self):
        # A positive context first (#2399 review F4): PR #40 is the recorded fix of issue #12,
        # so the drawer names #12 while the ledger can say so, and nothing once it cannot.
        run_id = self.run_row(12, "issue_fixer")
        self.run_row(40, "reviewer")
        now = time.time()
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO issue_fixes(run_id,repo,number,base,kind,branch,state,"
                        "pr_number,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, tid.REPO, 12, "main", "pr", "fix/12", "pushed", 40, now, now))
        self.assertEqual((self.view(12)["issue"], self.view(40)["issue"]), (12, 12))
        self.db, real = self.db.with_name("none.sqlite"), self.db
        for pr in (12, 40):                                         # no ledger at all
            self.assertNotIn("issue", self.view(pr))
        self.db = real
        with ledger.connect(self.db) as con:                        # an older ledger: no table
            con.execute("DROP TABLE issue_fixes")
        view = self.view(40)
        self.assertNotIn("issue", view)                             # unknown, not "none"
        self.assertEqual(view["turns"][-1]["seat"], "reviewer")     # the rest still reads


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
        for on in ("x", "-1", "5" * 13, "acme/other#5"):
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
