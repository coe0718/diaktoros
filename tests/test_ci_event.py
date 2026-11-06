"""#681: a finished CI run (``check_suite`` ``completed``) is acted on when GitHub reports it.

The delivery names a commit and nothing else is taken from it: the handler re-reads GitHub, then
runs the watchdog's own passes for that commit alone (``ci_fix.sweep``, ``main_check.sweep``) under
the sweep's lock. GitHub, CI, the notices and the queue are fakes; the passes are the real ones.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_main_check import FakeState  # noqa: E402
from diaktoros import (app_ingress, ci, ci_event, ci_fix, config, gate_decisions, gh, ledger,  # noqa: E402
                       main_check, observer)
from diaktoros.run_supervisor import CI_HOLD, Supervisor  # noqa: E402

HEAD, OLD, MAIN = "a" * 40, "b" * 40, "c" * 40
RED = ci.CIState(failed=["tests (3.11)"], ids={"tests (3.11)": 55},
                 urls={"tests (3.11)": "https://github.com/acme/widgets/runs/55"})
LOOP = {"id": "w", "repo": "acme/widgets", "cap": 3, "read_token": "reader", "base": "main",
        "fixers": ["fix"], "reviewers": ["rev"], "review_only": [], "required_checks": [],
        "unattended_fixer_push": True, "fix_ci": True, "ci_webhook": True}


def pr(number=7, head=HEAD, state="open"):
    return {"number": number, "state": state, "user": {"login": "fix"}, "base": {"ref": "main"},
            "head": {"sha": head}, "draft": False}


def suite(head=HEAD, numbers=(7,), action="completed"):
    return {"action": action, "repository": {"full_name": "acme/widgets"},
            "check_suite": {"head_sha": head, "conclusion": "failure",
                            "pull_requests": [{"number": n} for n in numbers]},
            "sender": {"login": "someone"}}


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
        self.notices, self.queued, self.reads = [], [], []
        self.prs = {7: pr()}
        self.main_head, self.state = MAIN, RED
        self.sup = Supervisor(Path(tmp.name) / "ledger.sqlite")
        self.woken = []

    def deliver(self, payload, loop=None):
        """One delivery through ``ci_event.gate``: ``(reason, decision)`` of its silence."""
        def api(_loop, path, **_kw):
            self.reads.append(path)
            return {"sha": self.main_head} if path.endswith("/commits/main") else {"commits": []}

        def pr_read(_loop, number):
            self.reads.append(f"pr {number}")
            return self.prs.get(number)

        def rows(_repo, _number, _db=None):   # the ledger: what this test queued so far
            return [{"head": a[3], "state": "pending"} for a, _kw in self.queued]

        with mock.patch.object(ci_event, "silence", side_effect=silenced), \
             mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(gh, "pr", side_effect=pr_read), \
             mock.patch.object(gh, "open_prs", side_effect=lambda _l: list(self.prs.values())), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch("diaktoros.gate.verdicts", return_value=[]), \
             mock.patch.object(ci, "read", side_effect=lambda _l, _h: self.state), \
             mock.patch.object(ci_fix, "rows", side_effect=rows), \
             mock.patch.object(observer, "notify",
                               side_effect=lambda *a, **kw: self.notices.append((a, kw))), \
             mock.patch("diaktoros.gate.enqueue_isolated",
                        side_effect=lambda *a, **kw: self.queued.append((a, kw)) or "enqueued"), \
             mock.patch("diaktoros.run_supervisor.production_ledger", return_value=self.sup.db), \
             mock.patch("diaktoros.gate.resume_isolated", side_effect=self.woken.append):
            if payload is None:                       # the watchdog's sweep, as its last step
                return ci_event.drain(loop or LOOP, self.st)
            with self.assertRaises(Silenced) as ended:
                ci_event.gate(loop or LOOP, self.st, payload)
        return ended.exception.reason, ended.exception.decision

    def held_review(self, head=HEAD, error=CI_HOLD + " on aaaaaaa — 1 check(s) still running"):
        """A reviewer turn parked for CI at ``head``, due again in 90 s: its id."""
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d-{head[:4]}-{len(error)}", "acme/widgets", 7, head, "reviewer",
                             turn_key=error[:5])
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=? WHERE head=? AND "
                        "retry_at IS NULL", (time.time() + 90, error, head))
            return con.execute("SELECT id FROM runs WHERE head=? AND error=?", (head, error)).fetchone()[0]

    def due(self, run_id):
        with ledger.connect(self.sup.db) as con:
            return con.execute("SELECT retry_at FROM runs WHERE id=?", (run_id,)).fetchone()[0] <= time.time()

    def test_a_red_suite_on_a_loop_prs_head_queues_one_ci_fix_turn_at_once(self):
        reason, decision = self.deliver(suite())
        self.assertEqual(decision, "accepted")
        self.assertIn("#7", reason)
        (args, kw), = self.queued
        self.assertEqual((args[1], args[2], args[3], kw["turn_key"]), ("fixer", 7, HEAD, ci_fix.KEY))
        (notice, _), = self.notices
        self.assertEqual(notice[2], "ci_failed")

    def test_several_suites_for_one_head_queue_one_turn(self):
        for _ in range(3):
            self.deliver(suite())
        self.assertEqual(len(self.queued), 1)

    def test_a_suite_on_a_superseded_head_queues_nothing(self):
        """The PR moved on: its current head, read from GitHub, is not the suite's commit."""
        self.prs = {7: pr(head=OLD)}
        reason, decision = self.deliver(suite(head=HEAD))
        self.assertEqual((self.queued, self.notices, decision), ([], [], "declined"))
        self.assertIn("not the base's head nor an open loop PR's head", reason)

    def test_a_closed_pr_or_one_the_loop_does_not_serve_queues_nothing(self):
        for found in (pr(state="closed"), {**pr(), "user": {"login": "stranger"}},
                      {**pr(), "draft": True}, None):
            with self.subTest(found=found):
                self.prs = {7: found}
                self.deliver(suite())
                self.assertEqual(self.queued, [])

    def test_a_red_suite_on_mains_head_sends_the_main_red_notice(self):
        self.prs = {}
        reason, decision = self.deliver(suite(head=MAIN, numbers=()))
        self.assertEqual(decision, "accepted")
        (args, kw), = self.notices
        self.assertEqual((args[2], args[4]), ("main_red", MAIN))
        self.assertIn("main's head", reason)
        self.assertEqual(self.queued, [])

    def test_a_red_main_that_a_rerun_turns_green_is_recorded_green(self):
        """Review on #1992 (F2): a head already said red is read again; green, it is recorded,
        so the next red names the merges since it."""
        self.prs = {}
        self.deliver(suite(head=MAIN, numbers=()))
        self.state = ci.CIState(passed=["tests (3.11)"])
        self.deliver(suite(head=MAIN, numbers=()))
        memory = self.st.kv_get(self.st.dir / main_check.FILE, {})
        self.assertEqual((memory.get("notified"), memory.get("green")), (MAIN, MAIN))
        self.assertEqual([a[2] for a, _ in self.notices], ["main_red"])   # said once

    def test_a_suite_on_an_older_main_commit_does_nothing(self):
        self.prs = {}
        self.deliver(suite(head=OLD, numbers=()))
        self.assertEqual(self.notices, [])

    def test_a_forks_pr_is_found_by_listing_the_open_prs(self):
        """A fork's suite names no PR: the open PRs are listed instead, and matched by head."""
        self.prs = {7: pr(head=OLD), 9: pr(number=9)}
        self.deliver(suite(numbers=()))
        self.assertEqual([a[2] for a, _ in self.queued], [9])

    def test_a_named_pr_is_read_once_however_often_it_is_named(self):
        self.deliver(suite(numbers=(7, 7)))
        self.assertEqual(self.reads.count("pr 7"), 1)

    def test_off_incomplete_or_shapeless_deliveries_read_nothing(self):
        for payload, loop, why in (
                (suite(), {**LOOP, "ci_webhook": False}, "ci_webhook is off"),
                (suite(action="requested"), None, "only a completed suite"),
                ({**suite(), "check_suite": {"head_sha": "not-a-sha"}}, None, "no head sha")):
            with self.subTest(why=why):
                reason, _ = self.deliver(payload, loop)
                self.assertIn(why, reason)
        self.assertEqual((self.reads, self.queued, self.notices), ([], [], []))

    def test_a_suite_that_lands_mid_sweep_is_acted_on_by_that_sweep_before_it_ends(self):
        """Review on #1992 (F4): the sweep may have read this head's CI before the suite finished,
        and no second delivery comes. The note makes the sweep's last step read it again."""
        fd = os.open(self.st.dir / "sweep.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)              # the watchdog's sweep, mid-pass
        with mock.patch.object(ci_event, "LOCK_WAIT_S", 0.2):
            reason, decision = self.deliver(suite())
        self.assertIn("noted for the running watchdog sweep", reason)
        self.assertEqual((self.reads, self.queued, decision), ([], [], "accepted"))
        acted = self.deliver(None)                  # the sweep's drain, still holding the lock
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(len(self.queued), 1)
        self.assertIn("CI read for #7", acted)
        self.assertEqual(self.deliver(None), [])    # drained once: nothing left for the next

    def test_a_handler_that_takes_the_lock_acts_on_every_note_once_per_commit(self):
        self.prs = {7: pr(), 9: pr(number=9, head=OLD)}
        ci_event.note(self.st, OLD, [9])           # left by a delivery that timed out
        ci_event.note(self.st, OLD, [9])
        self.deliver(suite())
        self.assertEqual(sorted(a[2] for a, _ in self.queued), [7, 9])
        self.assertEqual(self.reads.count("pr 9"), 1)

    def test_the_watchdog_drains_the_notes_before_it_lets_the_lock_go(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "scripts/watchdog.py"
        spec = importlib.util.spec_from_file_location("watchdog_681", path)
        watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watchdog)
        held = []

        def drain(_loop, st, _log):
            fd = os.open(st.dir / "sweep.lock", os.O_RDWR)
            try:                                    # still the sweep's: another taker is refused
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
        with mock.patch.object(watchdog, "_sweep_loop_locked", return_value=["swept"]), \
             mock.patch.object(watchdog.ci_event, "drain", side_effect=drain):
            self.assertEqual(watchdog.sweep_loop(LOOP, self.st), ["swept"])
        self.assertEqual(held, [True])

    def test_a_suite_after_a_ci_fix_wakes_the_review_held_for_ci_at_that_head(self):
        """Review on #1992 (F1): the review the CI-fix's push queued waits for CI; the finished
        suite makes it due now and runs the worker's recovery, whose claim reads CI again."""
        self.state = ci.CIState(passed=["tests (3.11)"])
        held, other, stale = (self.held_review(), self.held_review(head=OLD),
                              self.held_review(error="claim-time read failed"))
        reason, decision = self.deliver(suite())
        self.assertEqual(decision, "accepted")
        self.assertIn("reviews held for CI woken", reason)
        self.assertEqual((self.due(held), self.due(other), self.due(stale)), (True, False, False))
        self.assertEqual(len(self.woken), 1)
        self.assertEqual(self.queued, [])           # green: no CI fix

    def test_nothing_held_wakes_no_worker(self):
        self.state = ci.CIState(passed=["tests (3.11)"])
        reason, _ = self.deliver(suite())
        self.assertNotIn("woken", reason)
        self.assertEqual(self.woken, [])


class Routing(unittest.TestCase):
    def test_the_fixer_route_and_hook_carry_check_suite_only_when_on(self):
        self.assertEqual(config.hook_events(LOOP, "fixer"), ["pull_request_review", "check_suite"])
        self.assertEqual(config.hook_events({**LOOP, "ci_webhook": False}, "fixer"),
                         ["pull_request_review"])
        self.assertEqual(config.hook_events(LOOP, "reviewer"), ["pull_request"])

    def test_an_apps_suite_goes_to_the_fixer_gate_and_is_needed_only_when_on(self):
        self.assertEqual(app_ingress.gate_for(suite()), ("gate_fixer", ""))
        with mock.patch.object(config, "triage_enabled", return_value=False):
            self.assertIn("check_suite", app_ingress.events_needed(LOOP))
            self.assertNotIn("check_suite", app_ingress.events_needed({**LOOP, "ci_webhook": False}))

    def test_the_decision_record_names_the_event_and_the_commit(self):
        entry = gate_decisions._entry("gate_fixer", suite(), "accepted", "x")
        self.assertEqual((entry["event"], entry["head"]), ("check_suite", HEAD[:7]))

    def test_the_fixer_gate_hands_a_suite_to_the_handler_before_reading_a_review(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "scripts/gate_fixer.py"
        spec = importlib.util.spec_from_file_location("gate_fixer_681", path)
        gate_fixer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_fixer)
        handled = []
        with mock.patch("sys.stdin") as stdin, \
             mock.patch.object(gate_fixer.gate, "context", return_value=(LOOP, "st")), \
             mock.patch.object(ci_event, "gate",
                               side_effect=lambda *a: handled.append(a) or (_ for _ in ()).throw(SystemExit(0))):
            stdin.read.return_value = json.dumps(suite())
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        self.assertEqual(handled[0][2]["check_suite"]["head_sha"], HEAD)


import test_gate_shims as gs  # noqa: E402
from diaktoros.doctor import route_checks  # noqa: E402


class EveryPath(gs.Base):
    """``set --ci-webhook on`` on an installed loop: doctor names the fixer's route and hook as
    lacking ``check_suite``, and each printed fix, run as printed, leaves doctor green with the
    event on both. Turning it off asks for nothing."""

    REVIEW, FIX = gs.HookAndCronRemedies.REVIEW, gs.HookAndCronRemedies.FIX
    world, hook = gs.HookAndCronRemedies.world, staticmethod(gs.HookAndCronRemedies.hook)
    hook_checks, run_printed = gs.HookAndCronRemedies.hook_checks, gs.HookAndCronRemedies.run_printed

    def route_check(self):
        loop = config.load_id("widgets")
        return {c.name: c for c in route_checks.check_routes(loop)}["route:widgets-fix"]

    def test_turning_it_on_is_finished_by_the_printed_fixes(self):
        self.install()
        world = self.world([self.hook(1, self.REVIEW, ["pull_request"]),
                            self.hook(2, self.FIX, ["pull_request_review"])])
        self.assertFalse(self.route_check().failed)
        self.assertFalse(self.hook_checks()["hook:widgets-fix"].failed)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--ci-webhook", "on"])
        self.assertEqual(rc, 0, out)
        self.assertIs(config.load_id("widgets")["ci_webhook"], True)
        route, hook = self.route_check(), self.hook_checks()["hook:widgets-fix"]
        self.assertTrue(route.failed and hook.failed, (route.detail, hook.detail))
        self.assertIn("'check_suite'", route.detail)
        self.assertIn("'check_suite'", hook.detail)
        self.run_printed(route.fix)
        self.run_printed(self.hook_checks()["hook:widgets-fix"].fix)
        self.assertIn("check_suite", self.loop_routes()["widgets-fix"]["events"])
        live = {h["id"]: h["events"] for h in json.loads(world.read_text())["hooks"]}
        self.assertEqual(live, {1: ["pull_request"], 2: ["pull_request_review", "check_suite"]})
        after = self.hook_checks()
        self.assertFalse([c for c in after.values() if c.failed], {n: c.detail for n, c in after.items()})
        self.assertFalse(self.route_check().failed)

    def test_a_new_loop_with_it_on_gets_the_event_on_its_route(self):
        self.install("acme/widgets", "--ci-webhook", "on")
        self.assertEqual(self.loop_routes()["widgets-fix"]["events"],
                         ["pull_request_review", "check_suite"])
        self.assertEqual(self.loop_routes()["widgets-review"]["events"], ["pull_request"])


if __name__ == "__main__":
    unittest.main()
