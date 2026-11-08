"""#683: a change to the base's protection re-runs the protection check when GitHub reports it.

The delivery says only that a rule changed: the handler reads the base's rulesets and classic
protection again (``protection.read``, against a fake GitHub here) and names, in one ``protection``
notice, any check the loop expects that the base no longer requires. A rule that cannot cover the
base reads nothing.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main_check import FakeState  # noqa: E402
from diaktoros import (app_ingress, ci, config, gate_decisions, gate_shims, gh, observer,  # noqa: E402
                       protection_event, route_intent)

LOOP = {"id": "w", "repo": "acme/widgets", "base": "main", "read_token": "reader",
        "required_checks": ["tests", "lint"], "merge_check": "off", "protection_webhook": True}


def ruleset(include=("~DEFAULT_BRANCH",), exclude=(), target="branch", action="edited", **extra):
    return {"action": action, "repository": {"full_name": "acme/widgets", "default_branch": "main"},
            "repository_ruleset": {"id": 9, "name": "ci", "target": target,
                                   "conditions": {"ref_name": {"include": list(include),
                                                               "exclude": list(exclude)}}},
            "sender": {"login": "jeremy"}, **extra}


def classic(name="main", action="edited", **extra):
    return {"action": action, "repository": {"full_name": "acme/widgets", "default_branch": "main"},
            "rule": {"id": 3, "name": name}, "sender": {"login": "jeremy"}, **extra}


def checks_rule(*names):
    return {"type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": n} for n in names]}}


class Silenced(Exception):
    def __init__(self, reason, decision="declined"):
        super().__init__(reason)
        self.reason, self.decision = reason, decision


def silenced(reason, decision="declined"):
    raise Silenced(reason, decision)


class Handler(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.st = FakeState(tmp.name)
        self.rules = [checks_rule("tests", "lint")]       # what the base's rulesets require
        self.classic = None                               # no classic protection (404)
        self.reads, self.notices = [], []

    def request(self, _loop, path, **_kw):
        self.reads.append(path)
        if "/rules/branches/" in path:
            return (gh.Response(None, "HTTP 502", 502, {}) if self.rules == "unreadable" else
                    gh.Response(self.rules, "", 200, {}))
        if self.classic == "unreadable":
            return gh.Response(None, "HTTP 403", 403, {})
        return (gh.Response(None, "Branch not protected", 404, {}) if self.classic is None else
                gh.Response(self.classic, "", 200, {}))

    def deliver(self, payload, loop=None):
        """One delivery through ``protection_event.gate``: ``(reason, decision)`` of its silence."""
        with mock.patch.object(protection_event, "silence", side_effect=silenced), \
                mock.patch.object(gh, "request", side_effect=self.request), \
                mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **kw: self.notices.append((a, kw))), \
                self.assertRaises(Silenced) as ended:
            protection_event.gate(loop or LOOP, self.st, payload)
        return ended.exception.reason, ended.exception.decision

    def test_a_ruleset_change_that_drops_a_required_check_sends_one_notice_naming_it(self):
        self.assertEqual(self.deliver(ruleset())[1], "declined")     # still required: nothing
        self.assertEqual(self.notices, [])
        self.rules = [checks_rule("tests")]
        reason, decision = self.deliver(ruleset())
        self.assertEqual(decision, "accepted", reason)
        self.assertEqual(len(self.notices), 1)
        (args, kw), = self.notices
        self.assertEqual((args[2], args[3]), ("protection", 0))
        self.assertIn("main does not require lint", kw["outcome"])
        self.assertNotIn("tests", kw["outcome"])
        self.assertIn("by jeremy", kw["outcome"])
        self.deliver(ruleset())                    # GitHub's next event for the same edit
        self.assertEqual(len(self.notices), 1)

    def test_a_check_lost_again_after_it_came_back_is_named_again(self):
        for rules in (["tests"], ["tests", "lint"], ["tests"], []):
            self.rules = [checks_rule(*rules)]
            self.deliver(ruleset())
        self.assertEqual([kw["outcome"].split(" (")[0] for _a, kw in self.notices],
                         ["main does not require lint"] * 2 + ["main does not require lint, tests"])
        self.assertEqual(len({kw["identity"] for _a, kw in self.notices}), 3)   # 3 observer keys

    def test_a_classic_rule_change_is_read_from_classic_protection(self):
        self.rules = []
        self.classic = {"required_status_checks": {"contexts": ["tests"], "checks": []}}
        self.deliver(classic())
        self.assertIn("main does not require lint (", self.notices[0][1]["outcome"])

    def test_the_merge_check_is_expected_when_the_loop_posts_it(self):
        loop = {**LOOP, "merge_check": "on"}
        self.deliver(ruleset(), loop)
        self.assertIn(f"main does not require {ci.MERGE_CHECK}", self.notices[0][1]["outcome"])
        self.notices.clear()
        self.rules = [checks_rule("tests", "lint", ci.MERGE_CHECK)]
        self.assertIn("requires every check", self.deliver(ruleset(), loop)[0])
        self.assertEqual(self.notices, [])

    def test_a_change_on_another_branch_sends_nothing_and_reads_nothing(self):
        self.rules = []                            # it would be a notice, if it looked
        for payload in (classic(name="release/*"), classic(name="dev"),
                        ruleset(include=("refs/heads/release/*",)),
                        ruleset(include=("~ALL",), exclude=("refs/heads/main",)),
                        ruleset(include=("~ALL",), exclude=("refs/heads/m*",)),
                        ruleset(include=("~ALL",), exclude=("~DEFAULT_BRANCH",)),
                        ruleset(target="tag"), ruleset(target="push")):
            reason, decision = self.deliver(payload)
            self.assertEqual(decision, "declined", payload)
            self.assertIn("does not cover main", reason)
        reason, _ = self.deliver(ruleset(), {**LOOP, "base": "develop"})   # main is the default
        self.assertIn("does not cover develop", reason)
        self.assertEqual((self.notices, self.reads), ([], []))

    def test_a_rule_that_may_cover_the_base_is_read(self):
        self.rules = []
        for payload in (classic(name="*"), classic(name="ma?n"), classic(name="dev",
                                                                        changes={"name": {}}),
                        ruleset(include=("refs/heads/main",)), ruleset(include=("~ALL",)),
                        ruleset(include=("refs/heads/*",), exclude=("refs/heads/*/*",)),
                        ruleset(include=("~ALL",), exclude=("refs/*",)),     # * stays in a segment
                        ruleset(include=("~ALL",), exclude=("refs/heads/[m]ain",)),
                        ruleset(include=("refs/heads/dev",), changes={"conditions": {}}),
                        ruleset(action="deleted")):
            self.reads.clear()
            self.st.kv_set(self.st.dir / protection_event.FILE, [])
            self.assertEqual(self.deliver(payload)[1], "accepted", payload)
            self.assertTrue(self.reads, payload)

    def test_a_half_unread_is_said_and_wholly_unread_is_no_notice(self):
        self.rules = [checks_rule("tests")]
        self.classic = "unreadable"
        self.deliver(ruleset())
        self.assertIn("could not read classic branch protection", self.notices[0][1]["outcome"])
        self.notices.clear()
        self.st.kv_set(self.st.dir / protection_event.FILE, [])
        self.rules = "unreadable"                  # the ruleset read fails too
        reason, _ = self.deliver(ruleset())
        self.assertIn("could not read", reason)
        self.assertEqual(self.notices, [])

    def test_with_the_setting_off_or_nothing_expected_nothing_is_read(self):
        self.rules = []
        for loop in ({**LOOP, "protection_webhook": False},
                     {**LOOP, "required_checks": [], "merge_check": "off"}):
            self.deliver(ruleset(), loop)
        self.assertEqual((self.notices, self.reads), ([], []))


class Routing(unittest.TestCase):
    def test_the_reviewer_route_and_hook_carry_both_events_only_when_on(self):
        self.assertEqual(config.hook_events(LOOP, "reviewer"),
                         ["pull_request", "branch_protection_rule", "repository_ruleset"])
        self.assertEqual(config.hook_events({}, "reviewer"), ["pull_request"])
        self.assertEqual(config.hook_events(LOOP, "triage"), ["issues"])

    def test_a_reviewer_route_without_the_events_is_drift_only_when_on(self):
        entry = {"prompt": route_intent.ROUTE_PROMPT["reviewer"], "events": ["pull_request"]}
        self.assertIn("events", gate_shims.contract_drift(LOOP, "reviewer", entry))
        self.assertNotIn("events", gate_shims.contract_drift({}, "reviewer", entry))
        entry["events"] = ["pull_request", "branch_protection_rule", "repository_ruleset"]
        self.assertNotIn("events", gate_shims.contract_drift(LOOP, "reviewer", entry))
        app = {"prompt": route_intent.ROUTE_PROMPT["app"], "events": list(gate_shims.APP_EVENTS)}
        self.assertIn("events", gate_shims.contract_drift(LOOP, "app", app))

    def test_an_apps_protection_change_goes_to_the_reviewer_and_is_needed_only_when_on(self):
        self.assertEqual(app_ingress.gate_for(classic()), ("gate_reviewer", ""))
        self.assertEqual(app_ingress.gate_for(ruleset()), ("gate_reviewer", ""))
        for event in config.PROTECTION_EVENTS:
            self.assertIn(event, gate_shims.APP_ROUTE_EVENTS)
            self.assertIn(event, app_ingress.events_needed(LOOP))
            self.assertNotIn(event, app_ingress.events_needed({}))

    def test_the_decision_record_names_the_event(self):
        self.assertEqual(gate_decisions._entry("gate_reviewer", classic(), "declined", "x")["event"],
                         "branch_protection_rule")
        self.assertEqual(gate_decisions._entry("gate_reviewer", ruleset(), "declined", "x")["event"],
                         "repository_ruleset")

    def test_the_notice_is_urgent_and_labelled(self):
        self.assertIn("protection", observer.EVENTS)
        self.assertTrue(observer.is_urgent("protection"))
        self.assertIn("protection", observer.LABEL)
        self.assertIn("protection", observer.EMOJI)

    def test_the_reviewer_gate_hands_a_protection_change_to_the_handler(self):
        path = Path(__file__).resolve().parents[1] / "scripts/gate_reviewer.py"
        spec = importlib.util.spec_from_file_location("gate_reviewer_683", path)
        gate_reviewer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_reviewer)
        for payload in (classic(), ruleset()):
            handled = []
            with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(gate_reviewer.gate, "context", return_value=(LOOP, None)), \
                    mock.patch.object(protection_event, "gate",
                                      side_effect=lambda *a: handled.append(a)
                                      or (_ for _ in ()).throw(SystemExit(0))), \
                    mock.patch.object(gate_reviewer, "decide") as decide, \
                    self.assertRaises(SystemExit):
                gate_reviewer.main()
            self.assertEqual(handled, [(LOOP, None, payload)])
            decide.assert_not_called()


import test_gate_shims as gs  # noqa: E402


class SetPath(gs.Base):
    def test_set_turns_it_on_and_off_and_show_names_it(self):
        from diaktoros import show
        self.install()
        for value, want in (("on", True), ("off", False)):
            rc, out = self.run_cli(["set", "--loop", "widgets", "--protection-webhook", value])
            self.assertEqual(rc, 0, out)
            self.assertIs(config.load_id("widgets")["protection_webhook"], want)
        rows = {row.name: row for row in show._rows(config.load_id("widgets"))}
        self.assertEqual(rows["protection_webhook"].off, "`doctor` checks the base's protection")


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in their own modules."""
    suite = unittest.TestSuite()
    for cls in (Handler, Routing, SetPath):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
