"""#49: one isolated turn's wall clock is a per-loop, per-seat setting — not a hard 120 s.

The budget travels gate → ledger row → detached worker → Hermes's ``--run-budget`` and the
sandbox kill. These tests follow it along that path with the real pieces wherever they can run
without a network or a model: a real ``Supervisor`` spawning its real detached worker around a
fixture child, the real ``gate.enqueue_isolated`` writing a real ledger, and the real
``trusted_turn.run_turn`` building the real Hermes argv (only bwrap itself is faked).
"""
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from review_loop import (broker_ipc, cli, config, doctor, gate, gh,  # noqa: E402
                         run_supervisor, seat_model, selftest, trusted_turn)
from review_loop.run_supervisor import Supervisor  # noqa: E402
import test_selftest  # noqa: E402

HEAD = "a" * 40


def _loop(**extra) -> dict:
    raw = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
           "seats": {"reviewer": {"profile": "rev", "route": "r"},
                     "fixer": {"profile": "fix", "route": "f"}}}
    raw.update(extra)
    return raw


class Settings(unittest.TestCase):
    def test_default_is_a_real_review_not_two_minutes(self):
        loop = config.normalize(_loop())
        self.assertEqual(loop["turn_budget_s"], 900)
        for seat in ("reviewer", "fixer", "adjudicator"):
            self.assertEqual(config.turn_budget(loop, seat), 900)
        self.assertEqual(Supervisor.__init__.__kwdefaults__["child_timeout"], 900)
        self.assertEqual(config.SETTINGS_SCHEMA["turn_budget_s"]["default"], 900)
        self.assertEqual(config.settings_defaults(None)["turn_budget_s"], 900)

    def test_per_seat_wins_over_the_loop(self):
        raw = _loop(turn_budget_s=1200)
        raw["seats"]["fixer"]["turn_budget_s"] = "2400"
        loop = config.normalize(raw)
        self.assertEqual((config.turn_budget(loop, "reviewer"), config.turn_budget(loop, "fixer"),
                          config.turn_budget(loop, "adjudicator")), (1200, 2400, 1200))
        self.assertEqual(loop["seats"]["fixer"]["turn_budget_s"], 2400)   # stored as an int

    def test_an_unnormalized_loop_still_gets_the_default(self):
        self.assertEqual(config.turn_budget({"seats": {}}, "reviewer"), 900)

    def test_nonsense_is_refused(self):
        for bad in (0, 59, 14401, "soon", True, -5):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize(_loop(turn_budget_s=bad))
        raw = _loop()
        raw["seats"]["reviewer"]["turn_budget_s"] = 10
        with self.assertRaisesRegex(config.ConfigError, "seats.reviewer.turn_budget_s"):
            config.normalize(raw)

    def test_plugin_settings_feed_new_loops_and_apply(self):
        d = config.settings_defaults({"turn_budget_s": "1800"})
        self.assertEqual(d["turn_budget_s"], 1800)
        overlaid = config.apply_settings(_loop(turn_budget_s=900), {"turn_budget_s": 1800})
        self.assertEqual(config.normalize(overlaid)["turn_budget_s"], 1800)

    def test_plugin_yaml_declares_it(self):
        text = (ROOT / "plugin.yaml").read_text().split("\nconfig_schema:", 1)[1]
        block = text.split("\n  turn_budget_s:\n", 1)[1].split("\n  description:", 1)[0]
        self.assertIn("type: int", block)
        self.assertIn("default: 900", block)

    def test_cli_parses_init_set_and_selftest_flags(self):
        captured = {}

        class Ctx:
            def register_cli_command(self, name, help_text, setup, description=""):
                captured["setup"] = setup
        cli.register_cli(Ctx(), {"turn_budget_s": 1500})
        import argparse
        parser = argparse.ArgumentParser()
        captured["setup"](parser)
        init = parser.parse_args(["init", "--repo", "a/b", "--fixer-turn-budget", "3000"])
        self.assertEqual((init.turn_budget, init.reviewer_turn_budget, init.fixer_turn_budget),
                         (1500, None, 3000))   # the plugin setting is init's default
        change = parser.parse_args(["set", "--loop", "b", "--turn-budget", "1200",
                                    "--reviewer-turn-budget", "600"])
        self.assertEqual((change.turn_budget, change.reviewer_turn_budget), (1200, 600))
        self.assertIsNone(parser.parse_args(["selftest", "--loop", "b"]).timeout)


class CliSurfaces(unittest.TestCase):
    """init writes it, set changes it, status and doctor show it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes"),
                                             "REVIEW_LOOP_CONFIG_DIR": str(self.root / "loops")})
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "loops").mkdir()
        raw = _loop(turn_budget_s=900, state_dir=str(self.root / "state"))
        (self.root / "loops" / "widgets.json").write_text(json.dumps(raw))

    def run_cli(self, func, **kw):
        import argparse
        out = io.StringIO()
        with redirect_stdout(out):
            rc = func(argparse.Namespace(**kw))
        return rc, out.getvalue()

    def test_set_then_status_and_doctor_show_it(self):
        empty = dict(concurrency=None, cap=None, base=None, clone=None, grace_min=None,
                     marker_grace_min=None, ttl_min=None, inflight_ttl_min=None, host=None,
                     reviewer_concurrency=None, fixer_concurrency=None, adjudicator_login=None,
                     token=[], observer_route=None, observer_profile=None, observer_deliver=None,
                     observer_events=None, observer_digest_min=None, observer_mute=False,
                     observer_unmute=False, observer_disable=False)
        rc, out = self.run_cli(cli.cmd_set, loop="widgets", turn_budget=1200,
                               fixer_turn_budget=2400, reviewer_turn_budget=None, **empty)
        self.assertEqual(rc, 0, out)
        self.assertIn("turn_budget_s: 900 → 1200", out)
        self.assertIn("fixer turn budget: 900s → 2400s", out)
        loop = config.load_id("widgets")
        self.assertEqual((config.turn_budget(loop, "reviewer"), config.turn_budget(loop, "fixer")),
                         (1200, 2400))
        rc, out = self.run_cli(cli.cmd_set, loop="widgets", turn_budget=30,
                               fixer_turn_budget=None, reviewer_turn_budget=None, **empty)
        self.assertEqual(rc, 2)
        self.assertIn("60-14400", out)

        with mock.patch("review_loop.state.state_for") as state_for:
            state_for.return_value.dir = self.root / "state"
            state_for.return_value._load.return_value = {}
            state_for.return_value.queue_items.return_value = {}
            state_for.return_value.queue_all.return_value = {}
            state_for.return_value.breach_all.return_value = {}
            state_for.return_value.watch.return_value = {}
            rc, out = self.run_cli(cli.cmd_status, loop="widgets")
        self.assertIn("turn:       reviewer 1200s · fixer 2400s per turn", out)

        check = doctor.check_turn_budget(loop)
        self.assertEqual(check.status, doctor.UNKNOWN)          # the 2400s turn > the grace
        self.assertIn("reviewer 1200s · fixer 2400s", check.detail)
        self.assertIn("--grace-min 56", check.fix)          # 300 + 2400 + 30 + 600 s
        ok = doctor.check_turn_budget(config.normalize(_loop()))
        self.assertEqual((ok.status, ok.detail[:28]), (doctor.VERIFIED, "reviewer 900s · fixer 900s p"))


class DoctorWallClock(unittest.TestCase):
    """Every clock that judges a turn must fit the whole turn, not just the budget (#49, #51, #98).

    From launch to its end a turn may take the host dependency prefetch (deps.FETCH_TIMEOUT,
    before the budget starts), the budget, the sandbox kill grace after it, and then the broker
    drain (trusted_turn.BROKER_DRAIN_S) that lets an in-flight write finish — all inside the run.
    """

    def loop(self, budget=None, grace_min=None, ttl_min=None):
        loop = config.normalize(_loop())
        for key, value in (("turn_budget_s", budget), ("grace_min", grace_min),
                           ("ttl_min", ttl_min)):
            if value is not None:
                loop[key] = value
        return loop

    def check(self, budget, grace_min=25, ttl_min=None):
        return doctor.check_turn_budget(self.loop(budget, grace_min, ttl_min))

    @staticmethod
    def extra():
        from review_loop import deps
        return trusted_turn.KILL_GRACE_S + deps.FETCH_TIMEOUT + trusted_turn.BROKER_DRAIN_S

    def test_the_worst_case_counts_the_broker_drain(self):
        from review_loop import deps
        self.assertEqual(config.worst_turn_s(self.loop(900)), 900 + self.extra())
        self.assertEqual(900 + self.extra(), 1830)            # 300 + 900 + 30 + 600
        check = doctor.check_turn_budget(self.loop(900, 31))
        self.assertIn(f"up to 1830s launch to end ({deps.FETCH_TIMEOUT}s dependency prefetch + "
                      f"900s budget + {trusted_turn.KILL_GRACE_S}s kill grace + "
                      f"{trusted_turn.BROKER_DRAIN_S}s broker drain)", check.detail)

    def test_a_budget_under_the_grace_is_not_enough_when_the_turn_is_longer(self):
        check = self.check(1500)                     # 1500 s = the 25 min grace, exactly
        self.assertNotEqual(check.status, doctor.VERIFIED)
        worst = 1500 + self.extra()
        self.assertIn(f"{worst}s", check.detail)
        self.assertIn("dependency prefetch", check.detail)
        self.assertIn(f"--grace-min {-(-worst // 60)}", check.fix)

    def test_the_shipped_defaults_pass_their_own_check(self):
        import re
        loop = config.normalize(_loop())             # every default, as a fresh install has it
        check = doctor.check_turn_budget(loop)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertGreaterEqual(config.DEFAULTS["grace_min"] * 60, config.worst_turn_s(loop))
        self.assertEqual(config.SETTINGS_SCHEMA["grace_min"]["default"], config.DEFAULTS["grace_min"])
        yaml_text = (ROOT / "plugin.yaml").read_text()
        declared = re.search(r"\n  grace_min:\n(?:    .*\n)*?    default: (\d+)", yaml_text)
        self.assertEqual(int(declared.group(1)), config.DEFAULTS["grace_min"])

    def test_the_grace_boundary(self):
        grace = 31 * 60
        self.assertEqual(self.check(grace - self.extra(), 31).status, doctor.VERIFIED)
        self.assertNotEqual(self.check(grace - self.extra() + 1, 31).status, doctor.VERIFIED)

    def test_the_seat_lock_ttl_follows_a_turn_longer_than_ttl_min(self):
        at = 45 * 60 - self.extra()                  # the whole turn is exactly ttl_min
        self.assertEqual(config.seat_ttl_s(self.loop(at, 60, 45)), 45 * 60)
        self.assertEqual(config.seat_ttl_s(self.loop(at + 1, 60, 45)), 45 * 60 + 1)
        ceiling = self.loop(14400, 60, 45)
        self.assertEqual(config.seat_ttl_s(ceiling), 14400 + self.extra())
        self.assertEqual(config.seat_died_after_s(ceiling), 2 * (14400 + self.extra()))

    def test_doctor_compares_the_turn_with_every_threshold(self):
        ok = self.check(900, 31, 45)
        self.assertEqual(ok.status, doctor.VERIFIED, ok.detail)
        for text in ("watchdog grace 31m", "seat lock TTL 45m", "'that run died' after 90m"):
            self.assertIn(text, ok.detail)
        # A turn longer than ttl_min keeps its claim: the lock TTL follows the turn, and the
        # died report waits for twice that. Doctor says so; it is not a fault.
        long = self.check(4000, 90, 45)
        self.assertEqual(long.status, doctor.VERIFIED, long.detail)
        self.assertIn(f"seat lock TTL {-(-(4000 + self.extra()) // 60)}m (ttl_min 45m, raised to "
                      "fit the turn)", long.detail)
        # The grace is still a threshold the operator sets: too short is still a warning.
        short = self.check(4000, 60, 45)
        self.assertEqual(short.status, doctor.UNKNOWN)

    def test_a_healthy_long_turn_keeps_its_seat_claim(self):
        from review_loop import state as state_mod
        with tempfile.TemporaryDirectory() as tmp:
            loop = self.loop(14400, 300, 45)
            loop["state_dir"] = tmp
            st = state_mod.state_for(loop)
            now = time.time()
            st._save(st.locks, {"reviewer": {
                "acme/widgets#1": {"head": HEAD, "at": now - 50 * 60},           # past ttl_min
                "acme/widgets#2": {"head": HEAD, "at": now - config.seat_ttl_s(loop) - 60}}})
            self.assertEqual(set(st.live_locks("reviewer")), {"acme/widgets#1"})

    def test_the_watchdog_reports_a_died_run_only_past_twice_the_turn_ttl(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("watchdog_under_test",
                                                      ROOT / "scripts" / "watchdog.py")
        watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watchdog)
        loop = self.loop(14400, 300, 45)
        died = config.seat_died_after_s(loop)
        now = time.time()
        locks = {"reviewer": {"acme/widgets#1": {"at": now - 2 * 45 * 60 - 60},
                              "acme/widgets#2": {"at": now - died - 60}}}
        lines = watchdog.died_locks(loop, locks, now)
        self.assertEqual(len(lines), 1, lines)
        # Each line is ``(stable key, message)``: the sweep cools down on the key, so a persisting
        # mark warns once per window (#77), and the wording names the prune rather than a slot
        # that frees itself at ttl_min.
        key, line = lines[0]
        self.assertEqual(key, "lock:reviewer:acme/widgets#2")
        self.assertIn("acme/widgets#2", line)
        self.assertIn("the mark is pruned on the next sweep", line)
        self.assertNotIn("frees itself at", line)


def _watchdog():
    import importlib.util
    spec = importlib.util.spec_from_file_location("watchdog_under_test",
                                                  ROOT / "scripts" / "watchdog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarkerGraceFollowsTheRuling(unittest.TestCase):
    """#98 review 2, item 1: a breach marker's stall clock, through the real sweep (TEST off).

    ``awaiting-adjudication`` (no ruling run yet) is judged by ``marker_grace_min`` as before. An
    ``adjudicating`` marker has a ruling out: while its adjudicator run is live it is not a
    stall at all, and with no live run it is one only past the adjudicator's whole worst-case
    turn (or ``marker_grace_min``, if longer), counted from when the ruling started.
    """

    def setUp(self):
        from review_loop import state as state_mod
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "REVIEW_LOOP_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        self.state_mod = state_mod
        self.watchdog = _watchdog()
        self.watchdog.TEST = False                   # the real grace clocks

    def sweep(self, budget, status, age_s, run_state=None):
        # Each sweep gets its own loop state and run ledger (a marker keeps its first "at").
        self.calls = getattr(self, "calls", 0) + 1
        home = self.home / f"call-{self.calls}"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            return self._sweep(home, budget, status, age_s, run_state)

    def _sweep(self, home, budget, status, age_s, run_state):
        wd, now = self.watchdog, time.time()
        raw = _loop(state_dir=str(home / "loop-state"),
                    read_token="reviewer", cap=3,
                    adjudicator={"route": "widgets-breach", "profile": "default"})
        loop = config.normalize(raw)
        loop["seats"]["adjudicator"] = {"turn_budget_s": budget}
        st = self.state_mod.state_for(loop)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_s))
        st.breach_set(7, {"pr": 7, "head": HEAD, "rounds": 3, "status": status, "at": stamp,
                          "reason": "cap spent", **({"adjudicating_at": stamp}
                                                    if status == "adjudicating" else {})})
        st.watch_save({"armed_since": now - 90000, "heads": {"7": {
            "sha": HEAD, "base": "main", "base_sha": "b" * 40, "observed_at": now - 90000,
            "last_seen_at": now}}})
        ledger = Supervisor(home / "state" / "review-loop-runs.sqlite")
        if run_state:
            ledger.enqueue(f"adj-{budget}-{status}-{run_state}", loop["repo"], 7, HEAD,
                           "adjudicator", turn_key="breach:3", budget=budget)
            with sqlite3.connect(ledger.db) as con:
                con.execute("UPDATE runs SET state=? WHERE delivery=?",
                            (run_state, f"adj-{budget}-{status}-{run_state}"))
        pr = {"number": 7, "state": "open", "draft": False, "title": "t",
              "user": {"login": "fixer"}, "created_at": "2026-01-01T00:00:00Z",
              "base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": HEAD}}
        verdicts = [{"id": n, "state": "CHANGES_REQUESTED", "commit_id": HEAD,
                     "submitted_at": "2026-01-01T00:00:00Z",
                     "user": {"login": "reviewer", "id": 2}} for n in (1, 2, 3)]
        with mock.patch.object(wd.gate, "hooks_read", return_value=(True, "")), \
             mock.patch.object(wd.gh, "open_prs", return_value=[pr]), \
             mock.patch.object(wd.gh, "reviews", return_value=verdicts), \
             mock.patch.object(wd, "github_health", return_value=[]), \
             mock.patch.object(wd.route_intent, "heal", return_value=[]), \
             mock.patch.object(wd.gate, "resume_isolated", return_value=False), \
             mock.patch.object(wd, "reconcile_stacked"), \
             mock.patch.object(wd, "retry_fresh_reviews"), \
             mock.patch.object(wd, "retry_pending_breaches"), \
             mock.patch.object(wd, "drain_queued"), \
             mock.patch.object(wd.observer, "notify"), \
             mock.patch.object(wd.observer, "retry", return_value=False), \
             mock.patch.object(wd.observer, "flush"):
            lines = wd.sweep_loop(loop, st)
        # Every stall line about #7 (a young marker must not read as "no escalation marker").
        return [line for line in lines if "#7" in line]

    def test_a_ruling_in_flight_is_never_a_stall(self):
        for budget in (900, 7200, 14400):
            with self.subTest(budget=budget):
                self.assertEqual(self.sweep(budget, "adjudicating", 61 * 60, "running"), [])

    def test_a_ruling_with_no_live_run_waits_for_the_whole_adjudicator_turn(self):
        from review_loop import deps
        extra = trusted_turn.KILL_GRACE_S + deps.FETCH_TIMEOUT + trusted_turn.BROKER_DRAIN_S
        # 900 s: the whole turn (1830 s) is under marker_grace_min (60 m), which then decides.
        self.assertEqual(len(self.sweep(900, "adjudicating", 61 * 60)), 1)
        for budget in (7200, 14400):
            with self.subTest(budget=budget):
                self.assertEqual(self.sweep(budget, "adjudicating", 61 * 60), [])
                [line] = self.sweep(budget, "adjudicating", budget + extra + 120, "uncertain")
                self.assertIn("no adjudicator run is live (uncertain)", line)

    def test_a_marker_awaiting_its_run_keeps_marker_grace(self):
        for budget in (900, 7200, 14400):
            with self.subTest(budget=budget):
                [line] = self.sweep(budget, "awaiting-adjudication", 61 * 60)
                self.assertIn("parked awaiting adjudication", line)
                self.assertEqual(self.sweep(budget, "awaiting-adjudication", 59 * 60), [])

    def test_doctor_names_the_marker_clocks(self):
        loop = config.normalize(_loop(adjudicator={"route": "widgets-breach", "profile": "default"}))
        loop["seats"]["adjudicator"] = {"turn_budget_s": 7200}
        check = doctor.check_turn_budget(loop)
        self.assertIn("breach marker: awaiting-adjudication stalls after 60m; adjudicating only "
                      "with no live ruling run, after 136m", check.detail)

    def test_breach_start_stamps_when_the_ruling_started(self):
        from review_loop import state as state_mod
        loop = config.normalize(_loop(state_dir=str(self.home / "s")))
        st = state_mod.state_for(loop)
        st.breach_set(7, {"pr": 7, "head": HEAD, "rounds": 3, "status": "awaiting-adjudication",
                          "at": "2026-01-01T00:00:00Z"})
        st.breach_start(7, HEAD, 3)
        self.assertTrue(st.breach_get(7).get("adjudicating_at"))


class SeatClockFollowsTheRecordedBudget(unittest.TestCase):
    """#98 review 2, item 2: lowering turn_budget_s mid-turn must not free a live seat early."""

    def test_a_claim_keeps_the_budget_it_was_taken_with(self):
        from review_loop import state as state_mod
        with tempfile.TemporaryDirectory() as tmp:
            loop = config.normalize(_loop(state_dir=tmp, turn_budget_s=14400, ttl_min=45))
            st = state_mod.state_for(loop)
            st.acquire("reviewer", "acme/widgets#1", HEAD, "review")
            self.assertEqual(st._load(st.locks, {})["reviewer"]["acme/widgets#1"]["budget"], 14400)
            data = st._load(st.locks, {})
            data["reviewer"]["acme/widgets#1"]["at"] = time.time() - 60 * 60
            st._save(st.locks, data)
            loop["turn_budget_s"] = 60                 # lowered mid-turn
            self.assertLess(config.seat_ttl_s(loop), 60 * 60)
            self.assertIn("acme/widgets#1", st.live_locks("reviewer"))
            self.assertIn("acme/widgets#1", st.active("reviewer"))
            entry = st._load(st.locks, {})["reviewer"]["acme/widgets#1"]
            self.assertEqual(config.seat_ttl_s(loop, entry.get("budget")),
                             config.seat_ttl_s({**loop, "turn_budget_s": 14400}))
            # Nor is it reported dead on the lowered budget's clock.
            self.assertEqual(_watchdog().died_locks(loop, st._load(st.locks, {}), time.time()), [])

    def test_a_legacy_claim_without_a_budget_uses_the_loops(self):
        loop = config.normalize(_loop(turn_budget_s=900, ttl_min=45))
        self.assertEqual(config.seat_ttl_s(loop, None), config.seat_ttl_s(loop))
        self.assertEqual(config.seat_ttl_s(loop, 60), config.seat_ttl_s(loop))  # never shorter


class LedgerAndWorker(unittest.TestCase):
    """A real Supervisor, its real detached worker process, and a fixture child."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.db = self.root / "ledger.sqlite"
        self.seen = self.root / "seen"
        self.child = self.root / "child.py"
        # Records the budget the worker handed it, then sleeps for argv[2] seconds.
        self.child.write_text("import os,sys,time\nfrom pathlib import Path\n"
                              "Path(sys.argv[1]).write_text(os.environ.get('REVIEW_LOOP_TURN_BUDGET',''))\n"
                              "time.sleep(float(sys.argv[2]))\n")
        home = self.root / "home"
        home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(home), "HERMES_HOME": str(home)})
        patch.start()
        self.addCleanup(patch.stop)

    def sup(self, sleep: float, **kw) -> Supervisor:
        return Supervisor(self.db, fixture_mode=True,
                          fixture_command=[sys.executable, str(self.child), str(self.seen), str(sleep)],
                          **kw)

    def wait(self, sup, delivery, states, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            row = sup.get(delivery)
            if row and row["state"] in states:
                return row
            time.sleep(0.05)
        self.fail(f"{delivery} never reached {states}: {sup.get(delivery)}")

    def test_the_rows_budget_reaches_the_child_not_the_worker_default(self):
        sup = self.sup(0)
        self.assertEqual(sup.child_timeout, 900)   # no longer 120
        sup.enqueue("d1", "o/r", 1, HEAD, "reviewer", budget=1234)
        row = self.wait(sup, "d1", ("succeeded", "failed"))
        self.assertEqual((row["state"], row["budget"]), ("succeeded", 1234))
        self.assertEqual(self.seen.read_text(), "1234")

    def test_the_rows_budget_is_the_kill_deadline(self):
        # The worker is spawned with its 900 s default; only the row says 1 s.
        sup = self.sup(30)
        started = time.monotonic()
        sup.enqueue("d2", "o/r", 2, HEAD, "reviewer", budget=1)
        row = self.wait(sup, "d2", ("succeeded", "failed"))
        self.assertEqual((row["state"], row["error"]), ("failed", "child timeout"))
        self.assertLess(time.monotonic() - started, 12)

    def test_launch_lease_covers_the_whole_budget(self):
        sup = self.sup(0, lease_seconds=5)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d3", "o/r", 3, HEAD, "reviewer", budget=2000)
        seen = {}
        with mock.patch.object(sup, "_run_fixture",
                               side_effect=lambda run_id, owner, budget: seen.update(
                                   budget=budget, lease=sup.get("d3")["lease"] - time.time())):
            sup._run_one()
        self.assertEqual(seen["budget"], 2000)
        self.assertGreater(seen["lease"], 2000)

    def test_a_legacy_row_without_a_budget_uses_the_worker_value(self):
        sup = self.sup(0, child_timeout=77)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d4", "o/r", 4, HEAD, "reviewer")
        self.assertEqual(sup.get("d4")["budget"], 77)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET budget=NULL")
        self.assertEqual(sup.budget_of(sup.get("d4")["id"]), 77)

    def test_bad_budgets_are_refused_at_enqueue(self):
        sup = self.sup(0)
        for bad in (0, -1, True, "900"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sup.enqueue(f"bad-{bad!r}", "o/r", 5, HEAD, "reviewer", budget=bad)


class GateToProductionWorker(unittest.TestCase):
    """The gate records the loop's seat budget; the production worker hands it to the turn."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps({"source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}))
        runtime.chmod(0o600)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "REVIEW_LOOP_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        raw = _loop(turn_budget_s=1100, state_dir=str(self.home / "state"), read_token="reader")
        raw["seats"]["fixer"]["turn_budget_s"] = 2700
        # A fixer row is only written for a loop that admits unattended pushes (#72).
        raw["unattended_fixer_push"] = True
        self.loop = config.normalize(raw)
        by_repo = mock.patch.object(config, "by_repo", return_value=self.loop)
        by_repo.start()
        self.addCleanup(by_repo.stop)

    def ledger(self):
        return Supervisor(self.home / "state" / "review-loop-runs.sqlite")

    def test_enqueue_isolated_records_each_seats_budget(self):
        with mock.patch.object(Supervisor, "_spawn") as spawn:
            gate.enqueue_isolated(self.loop, "reviewer", 7, HEAD)
            gate.enqueue_isolated(self.loop, "fixer", 8, HEAD)
        self.assertTrue(spawn.called)          # a production worker would have been armed
        ledger = self.ledger()
        self.assertEqual(ledger.get(f"acme/widgets:7:{HEAD}:reviewer")["budget"], 1100)
        self.assertEqual(ledger.get(f"acme/widgets:8:{HEAD}:fixer")["budget"], 2700)

    def run_production(self, run_turn):
        with mock.patch.object(Supervisor, "_spawn"):
            gate.enqueue_isolated(self.loop, "fixer", 8, HEAD)
        sup = Supervisor(self.home / "state" / "review-loop-runs.sqlite",
                         production_config=self.home / "review-loop-runtime.json",
                         hermes_home=self.home)
        row = sup.get(f"acme/widgets:8:{HEAD}:fixer")
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1 WHERE id=?",
                        (row["id"],))
        inference = mock.Mock(upstream="u", key="k", model="m", api_mode="chat_completions",
                              proxy_model="", client_identity="")
        pr = {"number": 8, "head": {"sha": HEAD, "ref": "fix-8"}}
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(seat_model, "load_runtime", return_value={
                 "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
             mock.patch.object(seat_model, "resolve_seat", return_value=inference), \
             mock.patch.object(gh, "api", return_value=pr), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"), \
             mock.patch("review_loop.gate.latest_effective_review_at_head", return_value={}), \
             mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
             mock.patch.object(run_supervisor, "pr_change", return_value=None), \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            sup._run_production(row["id"], "w")
        return sup.get(f"acme/widgets:8:{HEAD}:fixer")

    def test_the_worker_hands_the_rows_budget_to_the_turn(self):
        seen = {}
        row = self.run_production(lambda _loop, scope, **kw: seen.update(kw) or 0)
        self.assertEqual(seen["timeout"], 2700)
        self.assertEqual(row["state"], "succeeded")

    def test_a_timeout_names_the_budget(self):
        def run_turn(_loop, scope, **kw):
            raise trusted_turn.TurnBudgetExceeded(["bwrap"], kw["timeout"], 30)
        row = self.run_production(run_turn)
        # Failed, not waiting (#53 x #49): the same budget would run out again. Re-armable.
        self.assertEqual((row["state"], row["retries"], row["retry_at"]), ("failed", 0, None))
        self.assertIn("killed at the 2700s turn budget (sandbox stopped 30s past it) — raise "
                      "turn_budget_s (hermes review-loop set --loop widgets "
                      "--fixer-turn-budget N), then `retry`", row["error"])
        with run_supervisor.Supervisor(self.home / "state" / "review-loop-runs.sqlite")._connect() as con:
            self.assertIsNone(run_supervisor.write_evidence(con, row["id"]))

    def test_a_budget_kill_reruns_on_the_raised_budget(self):
        # The operator raises the budget; a new event (or `retry`) re-arms the pre-write run
        # on it, not on the budget recorded when it was first enqueued.
        def run_turn(_loop, scope, **kw):
            raise trusted_turn.TurnBudgetExceeded(["bwrap"], kw["timeout"], 30)
        row = self.run_production(run_turn)
        self.assertEqual((row["state"], row["budget"]), ("failed", 2700))
        raised = config.normalize({**self.loop, "seats": {
            **self.loop["seats"], "fixer": {**self.loop["seats"]["fixer"], "turn_budget_s": 3600}}})
        with mock.patch.object(Supervisor, "_spawn"):
            self.assertEqual(gate.enqueue_isolated(raised, "fixer", 8, HEAD), "rearmed")
            again = self.ledger().get(f"acme/widgets:8:{HEAD}:fixer")
            self.assertEqual((again["state"], again["budget"]), ("pending", 3600))
            # A redelivery while it is pending does not change its terms.
            self.assertEqual(gate.enqueue_isolated(self.loop, "fixer", 8, HEAD), "pending")
        self.assertEqual(self.ledger().get(f"acme/widgets:8:{HEAD}:fixer")["budget"], 3600)
        sup = self.ledger()
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='failed'")
        self.assertEqual(sup.retry(again["id"], budget=4000), "pending")
        self.assertEqual(sup.get(f"acme/widgets:8:{HEAD}:fixer")["budget"], 4000)

    def test_a_budget_kill_after_a_completed_push_is_final_not_retry(self):
        # The drain lets an in-flight push finish after the sandbox is killed. Then the run
        # wrote: it is final (never replayed), and nothing may tell the operator "raise the
        # budget, then retry" — `retry` would refuse it.
        def run_turn(_loop, scope, **kw):
            sup = Supervisor(scope.ledger_db)
            sup.begin_push(scope.run_id, scope.repo, scope.number, scope.head)
            sup.confirm_push(scope.run_id, scope.repo, scope.number, scope.head)
            raise trusted_turn.TurnBudgetExceeded(["bwrap"], kw["timeout"], 30)
        with mock.patch.object(gh, "reviews", return_value=[]):
            row = self.run_production(run_turn)
        self.assertEqual((row["state"], row["retries"], row["retry_at"]), ("failed", 0, None))
        self.assertIn("killed at the 2700s turn budget", row["error"])
        self.assertIn("after it wrote (fixer push recorded)", row["error"])
        self.assertIn("final: never replayed; a new head gets a fresh turn", row["error"])
        for wrong in ("raise turn_budget_s", "then `retry`", "no external write"):
            self.assertNotIn(wrong, row["error"])
        sup = self.ledger()
        [view] = [r for r in sup.status() if r["id"] == row["id"]]
        line = run_supervisor.describe_run(view, "widgets")
        self.assertIn("may have written (fixer push recorded) — never replayed", line)
        for wrong in ("raise the turn budget", "re-arm", "no external write"):
            self.assertNotIn(wrong, line)
        with self.assertRaisesRegex(ValueError, "fixer push recorded"):
            sup.retry(row["id"])

    def test_a_drain_failure_after_a_budget_kill_names_both(self):
        # #98 review 2, item 3: the broker not shutting down (raised in run_turn's finally)
        # must not hide that the sandbox was killed at its budget first.
        def run_turn(_loop, scope, **kw):
            try:
                raise trusted_turn.TurnBudgetExceeded(["bwrap"], kw["timeout"], 30)
            except trusted_turn.TurnBudgetExceeded as exc:
                raise trusted_turn.TurnDenied(trusted_turn.drain_failure(exc)) from exc
        row = self.run_production(run_turn)
        self.assertEqual(row["state"], "uncertain")          # a write may still land
        self.assertIn("killed at the 2700s turn budget", row["error"])
        self.assertIn("broker did not shut down", row["error"])

    def test_another_timeout_is_not_blamed_on_the_budget(self):
        # Only the sandbox's own wall clock is the budget; a timeout elsewhere in the turn
        # (a host-side read, a helper) must not send the operator off to raise turn_budget_s.
        def run_turn(_loop, scope, **kw):
            raise subprocess.TimeoutExpired(["git"], 15)
        row = self.run_production(run_turn)
        # Any other pre-write timeout keeps #53's backed-off automatic retry.
        self.assertEqual((row["state"], row["retries"]), ("waiting", 1))
        self.assertNotIn("turn budget", row["error"])
        self.assertIn("TimeoutExpired", row["error"])


class RealTurnArgv(test_selftest.SelftestBase):
    """The real run_turn: the budget becomes --run-budget, and the kill comes a grace later."""

    def capture(self, delay_dispatch: float = 0.0, time_out: bool = False):
        seen = {}

        def run(**kwargs):
            if "broker_socket_dir" not in kwargs:           # step 2's sandbox probe
                return self.fx.contained_run(**kwargs)
            seen.update(entry=kwargs["entry"], timeout=kwargs["timeout"])
            sock = str(pathlib.Path(kwargs["broker_socket_dir"]) / "broker.sock")
            request = threading.Thread(target=lambda: seen.update(answer=broker_ipc.request(
                "review", verdict="REQUEST_CHANGES", body="late", socket_path=sock)), daemon=True)
            request.start()
            if not time_out:
                request.join()
                return subprocess.CompletedProcess([], 0, "done", "")
            dispatching.wait(5)
            # The sandbox is killed at its deadline while the broker is still mid-request.
            raise subprocess.TimeoutExpired(["bwrap"], kwargs["timeout"])

        dispatching = threading.Event()
        original = broker_ipc.RunBroker._dispatch

        def slow_dispatch(broker, raw):
            dispatching.set()
            time.sleep(delay_dispatch)
            seen["dispatched"] = True
            return original(broker, raw)
        stage = lambda loop, **kw: (kw["sandbox_root"].mkdir() or kw["sandbox_root"])  # noqa: E731
        from review_loop import broker, contained, review_receipt, trusted_fetch
        with mock.patch.object(trusted_fetch, "stage", side_effect=stage), \
             mock.patch.object(contained, "run", side_effect=run), \
             mock.patch.object(broker_ipc.RunBroker, "_dispatch", slow_dispatch), \
             mock.patch.object(broker, "perform", side_effect=AssertionError("perform called")), \
             mock.patch.object(review_receipt, "submit", side_effect=AssertionError("submit")):
            rc, text = self.run_selftest(pr=7, live_turn=True)   # no timeout: the loop's
        return seen, rc, text

    def test_selftest_uses_the_loops_reviewer_budget_and_argv_carries_it(self):
        self.fx.loop["turn_budget_s"] = 1500
        self.fx.loop["seats"]["reviewer"]["turn_budget_s"] = 777
        seen, rc, text = self.capture()
        self.assertEqual(rc, 0, text)
        entry = seen["entry"]
        self.assertEqual(entry[entry.index("--run-budget") + 1], "777")
        self.assertEqual(seen["timeout"], 777 + trusted_turn.KILL_GRACE_S)
        self.assertIn("up to 777s", text)

    def test_default_loop_selftest_is_the_production_default(self):
        seen, rc, text = self.capture()
        entry = seen["entry"]
        self.assertEqual(entry[entry.index("--run-budget") + 1], "900")

    def test_a_drain_that_outlives_its_bound_still_names_the_budget_kill(self):
        with mock.patch.object(trusted_turn, "BROKER_DRAIN_S", 1):
            seen, rc, text = self.capture(delay_dispatch=4, time_out=True)
        self.assertIn("broker did not shut down", text)
        self.assertIn("turn budget", text)

    def test_a_kill_mid_request_lets_the_broker_finish_instead_of_abandoning_it(self):
        # 6 s > the old 5 s join: before #49 this surfaced as "broker did not shut down" with
        # the request abandoned mid-write (for a fixer: an unresolved push intent → quarantine).
        seen, rc, text = self.capture(delay_dispatch=6, time_out=True)
        self.assertTrue(seen.get("dispatched"))
        self.assertIn("TurnBudgetExceeded", text)   # the sandbox clock, named as such
        self.assertNotIn("broker did not shut down", text)


if __name__ == "__main__":
    unittest.main()
