#!/usr/bin/env python3
"""Issue #75: a gate that crashes, overruns, or silences after a failed read is recorded,
alerted and re-driven — never indistinguishable from a deliberate ``[SILENT]``.

Every gate here runs the way the Hermes gateway runs a route script
(``webhook_filters.run_route_script``): ``[sys.executable, script]``, the payload as JSON on
stdin, ``cwd`` = the script's directory, a 30-second timeout.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from diaktoros import config, gate, gate_failures, state as state_mod  # noqa: E402

SCRIPTS = t.ROOT / "scripts"


def gateway_run(script: str, payload, extra_env: dict | None = None, kill_after: float = 30):
    """The gateway's contract, minus the HTTP: argv, stdin, cwd and its timeout (default 30s),
    after which the gateway kills the child — ``subprocess.TimeoutExpired`` here."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(SCRIPTS / script)], input=raw,
                          capture_output=True, text=True, cwd=str(SCRIPTS), timeout=kill_after,
                          env={**t.env(), **(extra_env or {})})
    return proc, time.monotonic() - started


def watchdog() -> str:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                          text=True, cwd=str(SCRIPTS), timeout=120, env=t.env())
    return proc.stdout


def loop_entries() -> dict:
    return gate_failures.Ledger(t.STATE_DIR).entries()


class GateFailureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})

    def test_malformed_payload_crash_is_recorded_without_a_loop(self):
        proc, _ = gateway_run("gate_reviewer.py", {"repository": "acme/widgets",
                                                   "action": "opened"})
        self.assertEqual(proc.returncode, 2)
        self.assertNotEqual(proc.stdout.strip(), "[SILENT]")
        self.assertIn("GATE FAILURE (crash)", proc.stderr)
        entries = gate_failures.fallback_ledger().entries()
        (entry,) = entries.values()
        self.assertEqual((entry["gate"], entry["kind"], entry["error_type"]),
                         ("gate_reviewer", "crash", "AttributeError"))
        self.assertIn("Traceback", entry["traceback"])
        self.assertLessEqual(len(entry["traceback"]), 4000)
        self.assertIn("gate failure", watchdog())

    def test_crash_is_recorded_alerted_redriven_and_resolved(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"          # GitHub answered a shape the gate chokes on
        t.set_prs({"7": broken})
        payload = t.pr_payload(7)
        proc, _ = gateway_run("gate_reviewer.py", payload)
        self.assertEqual(proc.returncode, 2)
        (key, entry), = loop_entries().items()
        self.assertEqual((entry["kind"], entry["repo"], entry["pr"], entry["head"],
                          entry["action"], entry["error_type"]),
                         ("crash", t.REPO, 7, t.HEAD_A, "review_requested", "AttributeError"))
        self.assertTrue(entry["payload_kept"] and entry["redrivable"])
        # explain names it as a blocker.
        loop = config.load_id("widgets")
        failures = gate_failures.open_for(loop, 7)
        self.assertEqual([f["id"] for f in failures], [key])
        self.assertIn(f"gate failure {key}", gate.gate_failure_line(failures[0]))

        # GitHub recovers; the watchdog alerts and re-drives the stored event.
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        out = watchdog()
        self.assertIn(f"gate failure {key}: gate_reviewer crash on #7", out)
        self.assertIn("re-driven — completed", out)
        self.assertTrue(loop_entries()[key]["resolved"])
        self.assertIn("re-driven by the watchdog", loop_entries()[key]["resolution"])
        self.assertEqual(gate_failures.open_for(loop, 7), [])
        self.assertNotIn(f"gate failure {key}", watchdog())   # resolved: said once

    def test_repeat_crash_is_deduped_and_redrives_are_capped(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"
        t.set_prs({"7": broken})
        payload = t.pr_payload(7)
        gateway_run("gate_reviewer.py", payload)
        gateway_run("gate_reviewer.py", payload)          # a manual redelivery: same event
        (key, entry), = loop_entries().items()
        self.assertEqual(entry["attempts"], 2)
        for _ in range(gate_failures.MAX_REDRIVES):
            self.assertIn("failed again", watchdog())
        out = watchdog()
        self.assertIn(f"gave up after {gate_failures.MAX_REDRIVES} re-drives", out)
        self.assertEqual(loop_entries()[key]["redrives"], gate_failures.MAX_REDRIVES)

    def test_hanging_github_is_a_recorded_timeout_well_inside_the_gateway_limit(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"DIAKTOROS_GH_STUB": str(hang),
                                     "DIAKTOROS_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 10)
        (entry,) = loop_entries().values()
        self.assertEqual((entry["kind"], entry["error_type"]), ("timeout", "GateBudgetExceeded"))

    def test_hang_outside_github_hits_the_backstop(self):
        # A lock another process never releases: not a GitHub read, so only SIGALRM ends it.
        import fcntl
        st = state_mod.LoopState(config.load_id("widgets"))
        st.dir.mkdir(parents=True, exist_ok=True)
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        with open(st.dir / "state.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                        {"DIAKTOROS_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 1 + gate_failures.BACKSTOP_S + 5)
        (entry,) = loop_entries().values()
        self.assertEqual(entry["kind"], "timeout")

    def test_silence_after_failed_read_is_incomplete_not_a_decision(self):
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        proc, _ = gateway_run("gate_reviewer.py", t.pr_payload(7),
                              {"DIAKTOROS_GH_STUB": str(failing)})
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "[SILENT]"))
        (key, entry), = loop_entries().items()
        self.assertEqual((entry["kind"], entry["error_type"]), ("incomplete", "GitHubReadFailed"))
        # One owner per failed read (#75 with #54): github-reads.json keeps it as the last failed
        # call, marked as owned by this entry, so the health sweep does not report it again.
        st = state_mod.LoopState(config.load_id("widgets"))
        failure = st.github_failure()
        self.assertEqual(failure.get("owned_by"), f"gate-failures:{key}")
        from unittest import mock
        from scripts import watchdog as wd
        ok = wd.gh.Response({"login": t.REVIEWER}, "", 200, {})
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gh, "auth_probe", return_value=ok):
            quiet = wd.github_health(config.load_id("widgets"), st, {}, time.time(), True, "")
            st.github_failure_record({k: v for k, v in failure.items() if k != "owned_by"})
            loud = wd.github_health(config.load_id("widgets"), st, {}, time.time(), True, "")
        self.assertEqual([line for line in quiet if "could not" in line], [])
        self.assertEqual(len([line for line in loud if "could not" in line]), 1)

    def test_deliberate_silence_records_nothing(self):
        proc, _ = gateway_run("gate_reviewer.py", t.pr_payload(7, action="labeled"))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "[SILENT]"))
        self.assertEqual(loop_entries(), {})

    def test_adjudicator_failures_are_alerted_never_redriven(self):
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.record("k1", {"gate": "gate_adjudicator", "kind": "crash", "pr": 7,
                             "redrivable": False, "error_type": "X", "error": "y"}, "{}")
        out = watchdog()
        self.assertIn("not re-driven (its output is a dispatch)", out)
        self.assertEqual(ledger.entries()["k1"]["redrives"], 0)

    # -- one sweep owns an entry's alert and re-drive ----------------------------------------
    #
    # ``init`` writes one cron job per loop and every job runs the same all-loops sweep, so two
    # sweeps overlapping on one ledger is the documented install, not an edge case.

    def slow_gate_dir(self, seconds: float = 1.0) -> tuple[pathlib.Path, pathlib.Path]:
        """A stand-in gate that logs each launch, takes ``seconds`` and fails again."""
        scripts = t.TMP / "slow-scripts"
        scripts.mkdir(exist_ok=True)
        launches = t.TMP / "launches.log"
        launches.write_text("")
        (scripts / "gate_reviewer.py").write_text(
            f"import os, time\nwith open({str(launches)!r}, 'a') as f:\n"
            "    f.write(str(os.getpid()) + '\\n')\n"
            f"time.sleep({seconds})\nraise SystemExit(2)\n")
        return scripts, launches

    def overlapping_sweeps(self, ledger, scripts, n: int = 2, cooldown_s: float = 3600.0):
        import threading
        barrier = threading.Barrier(n)
        out: list = [[] for _ in range(n)]

        def one(i):
            barrier.wait()
            out[i] = gate_failures.sweep(ledger, "[widgets]", scripts, cooldown_s=cooldown_s)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return [line for lines in out for line in lines]

    def redrivable_entry(self, key: str = "k1") -> gate_failures.Ledger:
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.record(key, {"gate": "gate_reviewer", "kind": "crash", "pr": 7, "redrivable": True,
                            "error_type": "X", "error": "y"}, json.dumps(t.pr_payload(7)))
        return ledger

    def test_overlapping_sweeps_alert_and_redrive_an_entry_once(self):
        ledger = self.redrivable_entry()
        scripts, launches = self.slow_gate_dir()
        lines = self.overlapping_sweeps(ledger, scripts)
        self.assertEqual(len(launches.read_text().split()), 1)
        self.assertEqual(len([line for line in lines if "gate failure k1" in line]), 1, lines)
        self.assertEqual(ledger.entries()["k1"]["redrives"], 1)
        self.assertNotIn("claim", ledger.entries()["k1"])          # released after the outcome
        # Round after round of three overlapping sweeps: the cap holds across all of them.
        for _ in range(gate_failures.MAX_REDRIVES + 1):
            self.overlapping_sweeps(ledger, scripts, n=3)
        self.assertEqual(len(launches.read_text().split()), gate_failures.MAX_REDRIVES)
        self.assertEqual(ledger.entries()["k1"]["redrives"], gate_failures.MAX_REDRIVES)

    def test_a_live_claim_is_respected_and_a_dead_sweeps_claim_expires(self):
        ledger = self.redrivable_entry()
        scripts, launches = self.slow_gate_dir(0)
        ledger.update("k1", {"claim": {"sweep": "other", "until": time.time() + 60}})
        self.assertEqual(gate_failures.sweep(ledger, "[w]", scripts, cooldown_s=0), [])
        self.assertEqual(launches.read_text(), "")
        # That sweep died holding it: once the lease runs out the entry is driven again.
        ledger.update("k1", {"claim": {"sweep": "dead", "until": time.time() - 1}})
        lines = gate_failures.sweep(ledger, "[w]", scripts, cooldown_s=0)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("failed again", lines[0])
        self.assertEqual(len(launches.read_text().split()), 1)
        self.assertNotIn("claim", ledger.entries()["k1"])

    def test_two_real_watchdogs_redrive_and_alert_once(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"
        t.set_prs({"7": broken})
        gateway_run("gate_reviewer.py", t.pr_payload(7))
        (key, _), = loop_entries().items()
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        # GitHub answers the PR slowly, so the first watchdog's re-drive is still running when
        # the second one reaches the ledger.
        slow = t.TMP / "slow_pr_stub.py"
        slow.write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                        "if sys.argv[1].endswith('/pulls/7'):\n    time.sleep(2)\n"
                        f"os.execv({str(t.STUB)!r}, [{str(t.STUB)!r}, *sys.argv[1:]])\n")
        slow.chmod(0o755)
        env = {**t.env(), "DIAKTOROS_GH_STUB": str(slow)}
        procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "watchdog.py")], text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=str(SCRIPTS), env=env) for _ in range(2)]
        outs = [proc.communicate(timeout=120)[0] for proc in procs]
        said = [line for out in outs for line in out.splitlines() if f"gate failure {key}" in line]
        self.assertEqual(len(said), 1, outs)
        self.assertIn("re-driven — completed", said[0])
        self.assertEqual(loop_entries()[key]["redrives"], 1)
        self.assertTrue(loop_entries()[key]["resolved"])

    # -- an unreadable ledger is kept for the operator, never overwritten ----------------------

    GARBAGE = '{"k0": {"gate": "gate_reviewer", "pr": 7, "attempts": 2, "resol'   # a torn write

    def corrupt_ledger(self) -> gate_failures.Ledger:
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.dir.mkdir(parents=True, exist_ok=True)
        for old in ledger.dir.glob(gate_failures.LEDGER + ".corrupt-*"):
            old.unlink()
        ledger.path.write_text(self.GARBAGE)
        return ledger

    def corrupt_copies(self) -> list[pathlib.Path]:
        return sorted(t.STATE_DIR.glob(gate_failures.LEDGER + ".corrupt-*"))

    def test_no_writer_overwrites_a_corrupt_ledger_or_writes_the_sentinel(self):
        writers = {
            "update": lambda ledger: ledger.update("k0", {"alerted_at": 1.0}),
            "resolve": lambda ledger: ledger.resolve("k0", "done"),
            "record": lambda ledger: ledger.record("k9", {"gate": "gate_fixer", "pr": 8}, "{}"),
            "sweep": lambda ledger: gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=0),
        }
        for name, write in writers.items():
            with self.subTest(writer=name):
                ledger = self.corrupt_ledger()
                write(ledger)
                write(ledger)                                 # a second write: still one copy
                (copy,) = self.corrupt_copies()
                self.assertEqual(copy.read_text(), self.GARBAGE)
                data = json.loads(ledger.path.read_text())
                self.assertFalse([k for k in data if k.startswith("_")], data)
                self.assertNotIn("k0", data)                  # nothing invented from the wreck

    def test_a_corrupt_ledger_is_alerted_once_shown_by_explain_and_clears_when_salvaged(self):
        self.corrupt_ledger()
        loop = config.load_id("widgets")
        before = gate_failures.open_for(loop, 7)             # explain, before any sweep ran
        self.assertEqual(len(before), 1)
        self.assertIn("unreadable", gate.gate_failure_line(before[0]))
        first, second = watchdog(), watchdog()
        (copy,) = self.corrupt_copies()
        alerts = [line for line in first.splitlines() if str(copy) in line]
        self.assertEqual(len(alerts), 1, first)
        self.assertIn("salvage", alerts[0])
        # (TEST mode's cooldown is 0, so the second sweep may say it again; the cooldown test
        # above pins the cadence.)
        self.assertEqual(copy.read_text(), self.GARBAGE)
        shown = [gate.gate_failure_line(e) for e in gate_failures.open_for(loop, 7)]
        self.assertEqual(len(shown), 1)
        self.assertIn(str(copy), shown[0])
        # A real gate failure still records, beside the kept copy.
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"
        t.set_prs({"7": broken})
        self.assertEqual(gateway_run("gate_reviewer.py", t.pr_payload(7))[0].returncode, 2)
        self.assertEqual(len([e for e in loop_entries().values() if e.get("gate") == "gate_reviewer"]), 1)
        # The operator salvages what they need and deletes the copy: the entry clears itself.
        copy.unlink()
        watchdog()
        self.assertFalse([e for e in gate_failures.open_for(loop, 7) if e.get("kind") == "ledger"])

    # A file that parses but is not a ledger — the stand-in a run at <= 07b0b01 could persist,
    # a list, an entry that is not an object — is as unreadable as a torn write.
    POISONED = {
        "stand-in": json.dumps({"_unreadable": {"gate": "?", "kind": "ledger", "resolved": False,
                                                "error": "x is unreadable", "attempts": 1}}),
        "list": "[]",
        "entry not an object": json.dumps({"k0": 5}),
    }

    def test_a_readable_but_invalid_ledger_is_moved_aside_not_bricked(self):
        writers = {
            "record": lambda ledger: ledger.record("k9", {"gate": "gate_fixer", "pr": 8}, "{}"),
            "resolve": lambda ledger: ledger.resolve("k0", "done"),
            "update": lambda ledger: ledger.update("k0", {"alerted_at": 1.0}),
            "sweep": lambda ledger: gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=0),
        }
        for shape, text in self.POISONED.items():
            for name, write in writers.items():
                with self.subTest(shape=shape, writer=name):
                    ledger = self.corrupt_ledger()
                    ledger.path.write_text(text)
                    write(ledger)
                    write(ledger)
                    (copy,) = self.corrupt_copies()
                    self.assertEqual(copy.read_text(), text)
                    data = json.loads(ledger.path.read_text())
                    self.assertFalse([k for k in data if k.startswith("_")], data)
                    self.assertNotIn(gate_failures.UNREADABLE, ledger.entries())
                    (stand_in,) = [e for e in data.values() if e.get("kind") == "ledger"]
                    self.assertEqual(stand_in["corrupt_copy"], str(copy))

    def test_the_pointer_to_a_corrupt_copy_survives_the_entry_bound(self):
        ledger = self.corrupt_ledger()
        ledger.snapshot()                                   # moved aside; the pointer is written
        for i in range(gate_failures.MAX_ENTRIES + 5):
            ledger.record(f"k{i}", {"gate": "gate_reviewer", "pr": 7}, "{}")
        pointers = [e for e in ledger.entries().values() if e.get("kind") == "ledger"]
        self.assertEqual(len(pointers), 1)
        self.assertLessEqual(len(ledger.entries()), gate_failures.MAX_ENTRIES)

    def test_a_crash_between_move_aside_steps_leaves_it_recoverable(self):
        from unittest import mock
        from diaktoros import state as st
        real = st._atomic_write
        for step in ("fresh save", "copy"):
            with self.subTest(step=step):
                ledger = self.corrupt_ledger()
                boom = OSError(f"crash during {step}")
                patch = (mock.patch.object(st, "_atomic_write", side_effect=boom) if step == "fresh save"
                         else mock.patch.object(gate_failures.os, "link", side_effect=boom))
                if step == "copy":           # no hard link and no room for a copy either
                    patch2 = mock.patch.object(gate_failures, "_write_copy", side_effect=boom)
                else:
                    patch2 = mock.patch.object(gate_failures, "_noop", create=True)
                with patch, patch2, self.assertRaises(Exception):
                    ledger.record("k1", {"gate": "gate_reviewer", "pr": 7}, "{}")
                # Nothing lost: the original still sits where it was, byte for byte.
                self.assertEqual(ledger.path.read_text(), self.GARBAGE)
                self.assertLessEqual(len(self.corrupt_copies()), 1)
                # The next writer finishes the job — one copy, a fresh ledger that points to it.
                self.assertIs(st._atomic_write, real)
                ledger.record("k1", {"gate": "gate_reviewer", "pr": 7}, "{}")
                (copy,) = self.corrupt_copies()
                self.assertEqual(copy.read_text(), self.GARBAGE)
                data = json.loads(ledger.path.read_text())
                self.assertIn("k1", data)
                self.assertEqual([e["corrupt_copy"] for e in data.values()
                                  if e.get("kind") == "ledger"], [str(copy)])

    def test_a_missing_kept_payload_says_so_and_how_to_redeliver(self):
        ledger = self.redrivable_entry()
        (ledger.payload_dir / "k1.json").unlink()
        scripts, launches = self.slow_gate_dir(0)
        (line,) = gate_failures.sweep(ledger, "[w]", scripts, cooldown_s=0)
        shown = gate.gate_failure_line(ledger.open_for(7)[0])
        for said in (line, shown):
            self.assertNotIn("GitHub is not answering", said)
            self.assertNotIn("retries it", said)
            self.assertIn("stored payload is missing", said)
            self.assertIn("cannot be re-driven", said)
            self.assertIn("Redeliver", said)
        self.assertEqual(launches.read_text(), "")
        self.assertEqual(ledger.entries()["k1"]["redrives"], 0)

    # -- a payload that was not kept is never promised a retry ---------------------------------

    def test_a_payload_not_kept_says_how_to_redeliver_it(self):
        broken = t.pr(7, requested=t.SEAT)
        broken["head"] = "not-an-object"
        t.set_prs({"7": broken})
        payload = {**t.pr_payload(7), "padding": "x" * gate_failures.MAX_PAYLOAD_BYTES}
        proc, _ = gateway_run("gate_reviewer.py", payload)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("re-drives it", proc.stderr)
        (key, entry), = loop_entries().items()
        self.assertFalse(entry["payload_kept"])
        route = config.load_id("widgets")["seats"]["reviewer"]["route"]
        line = gate.gate_failure_line(gate_failures.open_for(config.load_id("widgets"), 7)[0])
        out = [x for x in watchdog().splitlines() if f"gate failure {key}" in x]
        self.assertEqual(len(out), 1)
        for said in (line, out[0]):
            self.assertNotIn("retries it", said)
            self.assertNotIn("not re-driven this sweep", said)
            self.assertIn("cannot be re-driven", said)
            self.assertIn(f"webhooks/{route}", said)
            self.assertIn("Recent Deliveries", said)
            self.assertIn("Redeliver", said)
        self.assertEqual(loop_entries()[key]["redrives"], 0)

    # -- the gate's budget fits the gateway's script timeout ---------------------------------

    def gateway_config(self, data: dict | None, legacy: dict | None = None) -> None:
        cfg, gw = t.HOME / "config.yaml", t.HOME / "gateway.json"
        for path, value in ((cfg, data), (gw, legacy)):
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(value))       # JSON is valid YAML
        self.addCleanup(cfg.unlink, missing_ok=True)
        self.addCleanup(gw.unlink, missing_ok=True)

    def test_gateway_timeout_is_read_the_way_the_gateway_merges_it(self):
        self.gateway_config(None)
        self.assertEqual(gate_failures.gateway_script_timeout(),
                         (30, "gateway default (not set)"))
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}},
                            {"platforms": {"webhook": {"script_timeout_seconds": 90}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 10)   # yaml beats gateway.json
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10,
                                                       "extra": {"script_timeout_seconds": 45}}},
                             "gateway": {"webhook": {"script_timeout_seconds": 12}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 45)   # extra wins
        self.gateway_config({"gateway": {"webhook": {"script_timeout_seconds": 12}}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 12)
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": "soon"}}})
        self.assertIsNone(gate_failures.gateway_script_timeout()[0])
        from unittest import mock
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}})
        with mock.patch.object(gate_failures, "_read_yaml",
                               side_effect=gate_failures._NoYamlReader):
            timeout, why = gate_failures.gateway_script_timeout()
        self.assertIsNone(timeout)
        self.assertIn("no YAML reader", why)

    def profile_config(self, name: str, data: dict) -> pathlib.Path:
        """A profile home's config.yaml (JSON is valid YAML), restored after the test."""
        home = t.HOME / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        path = home / "config.yaml"
        before = path.read_text() if path.exists() else None
        path.write_text(json.dumps({"model": {"default": "test-model"}, **data}))
        self.addCleanup(lambda: path.write_text(before) if before is not None
                        else path.unlink(missing_ok=True))
        return home

    def doctor_lines(self):
        from diaktoros import doctor
        return {c.name: c for c in doctor.check_gate_timeouts(config.load_id("widgets"))}

    def test_doctor_checks_every_profile_gateway_hosting_a_loop_route(self):
        from diaktoros import doctor
        # Root (host) gateway: 30s by default. The reviewer's profile runs its own standalone
        # gateway at 10s; the fixer's profile is multiplexed by the host but sets 60s itself.
        self.gateway_config(None)
        rev = self.profile_config("reviewer-profile", {
            "gateway": {"standalone": True},
            "platforms": {"webhook": {"script_timeout_seconds": 10}}})
        self.profile_config("fixer-profile", {
            "platforms": {"webhook": {"script_timeout_seconds": 60}}})
        checks = self.doctor_lines()
        self.assertEqual(sorted(checks), ["gate:timeout:default", "gate:timeout:fixer-profile",
                                          "gate:timeout:reviewer-profile"])
        low = checks["gate:timeout:reviewer-profile"]
        self.assertEqual(low.status, doctor.MISMATCH)
        self.assertIn("serves reviewer", low.detail)
        self.assertIn("profile reviewer-profile's standalone gateway: 10s", low.detail)
        self.assertIn(f"in {rev / 'config.yaml'} for the profile reviewer-profile's standalone "
                      f"gateway", low.fix)
        fixer = checks["gate:timeout:fixer-profile"]
        self.assertEqual(fixer.status, doctor.VERIFIED, fixer.detail)
        self.assertIn("own gateway (if it runs one): 60s", fixer.detail)
        self.assertIn("host gateway multiplexing profile fixer-profile: 30s", fixer.detail)
        self.assertEqual(checks["gate:timeout:default"].status, doctor.VERIFIED)
        self.assertIn("serves adjudicator", checks["gate:timeout:default"].detail)

        # Now the host gateway drops to 20s: the multiplexed fixer profile and the default
        # profile are flagged against the ROOT file; the standalone reviewer is unaffected by it.
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 20}}})
        checks = self.doctor_lines()
        fixer = checks["gate:timeout:fixer-profile"]
        self.assertEqual(fixer.status, doctor.MISMATCH)
        self.assertIn(f"in {t.HOME / 'config.yaml'} for the host gateway", fixer.fix)
        self.assertNotIn("fixer-profile/config.yaml", fixer.fix)
        self.assertEqual(checks["gate:timeout:default"].status, doctor.MISMATCH)
        self.assertNotIn(str(t.HOME / "config.yaml") + " ",
                         checks["gate:timeout:reviewer-profile"].fix)
        self.assertIn("reviewer-profile/config.yaml", checks["gate:timeout:reviewer-profile"].fix)

        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": "x"}}})
        self.assertEqual(self.doctor_lines()["gate:timeout:default"].status, doctor.UNKNOWN)

    def test_gate_reads_the_gateway_it_runs_under(self):
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 45}}})
        solo = self.profile_config("solo", {"gateway": {"standalone": True},
                                            "platforms": {"webhook": {"script_timeout_seconds": 12}}})
        muxed = self.profile_config("muxed", {"platforms": {"webhook": {"script_timeout_seconds": 90}}})
        self.assertEqual(gate_failures.effective_timeout(t.HOME)[0], 45)       # root gateway
        self.assertEqual(gate_failures.effective_timeout(solo)[0], 12)         # its own only
        self.assertEqual(gate_failures.effective_timeout(muxed)[0], 45)        # host is lower
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 200}}})
        self.assertEqual(gate_failures.effective_timeout(muxed)[0], 90)        # own is lower
        # The real gate, run as a standalone profile gateway runs it: HERMES_HOME = the profile.
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"DIAKTOROS_GH_STUB": str(hang), "HERMES_HOME": str(solo)})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 12)
        (entry,) = loop_entries().values()
        self.assertAlmostEqual(entry["budget_s"], gate_failures.plan(12)[0])  # 12s, fitted

    def test_gate_shrinks_its_budget_to_a_low_gateway_timeout(self):
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}})
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                    {"DIAKTOROS_GH_STUB": str(hang)})
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 10)                      # inside the gateway's 10s kill
        (entry,) = loop_entries().values()
        self.assertEqual(entry["kind"], "timeout")
        self.assertAlmostEqual(entry["budget_s"], gate_failures.plan(10)[0])

    # -- a failed gate never keeps a seat ------------------------------------------------------

    # -- every layer the gateway reads (Arbiter on 7cde354) -------------------------------------

    def hang_stub(self) -> pathlib.Path:
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        return hang

    def test_a_top_level_webhook_block_is_read_like_the_gateway_reads_it(self):
        cfg = t.HOME / "config.yaml"
        self.gateway_config({"webhook": {"script_timeout_seconds": 6}})
        self.assertEqual(gate_failures.gateway_script_timeout(), (6, f"{cfg} webhook"))
        # The top-level block is bridged into ``extra`` last: it beats every nested block ...
        self.gateway_config({"platforms": {"webhook": {"extra": {"script_timeout_seconds": 45}}},
                             "gateway": {"webhook": {"script_timeout_seconds": 50}},
                             "webhook": {"script_timeout_seconds": 12}})
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 12)
        # ... and inside it, its own ``extra`` beats its plain key.
        self.gateway_config({"webhook": {"script_timeout_seconds": 12,
                                         "extra": {"script_timeout_seconds": 9}}})
        self.assertEqual(gate_failures.gateway_script_timeout(), (9, f"{cfg} webhook.extra"))
        # doctor does not certify a 6s gateway as fitting the default budget.
        from diaktoros import doctor
        self.gateway_config({"webhook": {"script_timeout_seconds": 6}})
        self.assertEqual(self.doctor_lines()["gate:timeout:default"].status, doctor.MISMATCH)

    def test_a_gateway_killing_at_a_top_level_6s_still_gets_a_recorded_timeout(self):
        # Arbiter's end-to-end repro: the gateway kills the child at 6s; the gate hangs in a read.
        self.gateway_config({"webhook": {"script_timeout_seconds": 6}})
        try:
            proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                        {"DIAKTOROS_GH_STUB": str(self.hang_stub())},
                                        kill_after=6)
        except subprocess.TimeoutExpired:
            self.fail("the gateway killed the gate at 6s before it recorded anything")
        self.assertEqual(proc.returncode, 3)
        self.assertLess(elapsed, 6)
        (entry,) = loop_entries().values()
        self.assertEqual(entry["kind"], "timeout")
        self.assertAlmostEqual(entry["budget_s"], gate_failures.plan(6)[0])   # 6s, fitted

    def test_a_malformed_gateway_json_is_skipped_like_the_gateway_skips_it(self):
        legacy = t.HOME / "gateway.json"
        self.addCleanup(legacy.unlink, missing_ok=True)
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 10}}})
        legacy.write_text('{"platforms": {"webhook": {"script_timeout_seconds": 90')   # torn
        seconds, where = gate_failures.gateway_script_timeout()
        self.assertEqual(seconds, 10)                    # config.yaml still decides
        self.assertIn("gateway.json unreadable, ignored as the gateway ignores it", where)
        self.gateway_config(None)
        legacy.write_text('{"platforms": {"webhook": {"script_timeout_seconds": 90')
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 30)

    def test_the_managed_overlay_wins_over_the_users_config(self):
        managed = t.TMP / "managed"
        managed.mkdir(exist_ok=True)
        (managed / "config.yaml").write_text(json.dumps({"webhook": {"script_timeout_seconds": 7}}))
        self.gateway_config({"webhook": {"script_timeout_seconds": 12}})
        from unittest import mock
        with mock.patch.dict(os.environ, {"HERMES_MANAGED_DIR": str(managed)}):
            self.assertEqual(gate_failures.gateway_script_timeout(),
                             (7, f"{managed / 'config.yaml'} webhook"))

    # -- a config the gateway cannot decode is skipped, as the gateway skips it --------------

    UNDECODABLE = {
        "latin-1": ('# caf\xe9\n' + json.dumps(
            {"platforms": {"webhook": {"script_timeout_seconds": 20}}})).encode("latin-1"),
        "utf-16": json.dumps(
            {"platforms": {"webhook": {"script_timeout_seconds": 20}}}).encode("utf-16"),
    }

    def raw_config(self, home: pathlib.Path, data: bytes | None, legacy: dict | None = None):
        home.mkdir(parents=True, exist_ok=True)
        cfg, gw = home / "config.yaml", home / "gateway.json"
        before = cfg.read_bytes() if cfg.exists() else None
        cfg.write_bytes(data) if data is not None else cfg.unlink(missing_ok=True)
        gw.write_text(json.dumps(legacy)) if legacy is not None else gw.unlink(missing_ok=True)
        self.addCleanup(lambda: cfg.write_bytes(before) if before is not None
                        else cfg.unlink(missing_ok=True))
        self.addCleanup(gw.unlink, missing_ok=True)

    def test_an_undecodable_config_yaml_falls_back_to_gateway_json_end_to_end(self):
        # Arbiter's repro: config.yaml names 20 but cannot be decoded, so the real gateway drops it
        # and gateway.json's 6 wins. The gate must plan under 6s and record the timeout.
        for encoding, data in self.UNDECODABLE.items():
            with self.subTest(encoding=encoding):
                t.reset(prs={})
                self.raw_config(t.HOME, data,
                                {"platforms": {"webhook": {"script_timeout_seconds": 6}}})
                seconds, where = gate_failures.gateway_script_timeout()
                self.assertEqual(seconds, 6)
                self.assertIn("cannot be decoded", where)
                try:
                    proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                                {"DIAKTOROS_GH_STUB": str(self.hang_stub())},
                                                kill_after=6)
                except subprocess.TimeoutExpired:
                    self.fail("the gateway killed the gate at 6s before it recorded anything")
                self.assertEqual(proc.returncode, 3)
                (entry,) = loop_entries().values()
                self.assertEqual(entry["kind"], "timeout")

    def test_a_junk_profile_config_never_raises_out_of_the_fit_or_doctor(self):
        from diaktoros import doctor
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 12}}})
        junk = t.HOME / "profiles" / "reviewer-profile"
        self.raw_config(junk, b"\xff\xfe\x00\x81junk\x00")
        limit, rows = gate_failures.effective_timeout(junk)
        self.assertEqual(limit, 12)                    # the host's readable, lower limit
        self.assertEqual(len(rows), 2)
        checks = self.doctor_lines()                   # does not raise
        self.assertIn("gate:timeout:reviewer-profile", checks)

    def test_doctor_reports_a_fit_check_that_cannot_run_instead_of_dying(self):
        from unittest import mock
        from diaktoros import doctor
        with mock.patch.object(gate_failures, "effective_timeout",
                               side_effect=RuntimeError("boom")):
            checks = self.doctor_lines()
        self.assertTrue(checks)
        self.assertTrue(all(c.status == doctor.UNKNOWN for c in checks.values()))
        self.assertIn("RuntimeError: boom", next(iter(checks.values())).detail)

    # -- the record phase always has its time ---------------------------------------------------

    def test_plan_always_leaves_start_up_and_the_record_phase(self):
        for timeout in (3, 5, 6, 6.7, 8, 10, 12, 13, 20, 28, 30, 60):
            budget, backstop = gate_failures.plan(timeout)
            spent = gate_failures.STARTUP_S + budget + backstop + gate_failures.RECORD_S
            if timeout >= gate_failures.MIN_RECORDABLE_S:
                self.assertLessEqual(spent, timeout, (timeout, budget, backstop))
            self.assertGreater(budget, 0)
        self.assertEqual(gate_failures.plan(30), (20.0, 3.0))

    def test_doctor_and_plan_agree_on_the_full_budget_threshold(self):
        from diaktoros import doctor
        for seconds in range(24, 31):
            with self.subTest(timeout=seconds):
                self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": seconds}}})
                full = gate_failures.plan(seconds, gate_failures.DEFAULT_BUDGET_S) == (
                    gate_failures.DEFAULT_BUDGET_S, gate_failures.BACKSTOP_S)
                status = self.doctor_lines()["gate:timeout:default"].status
                self.assertEqual(status == doctor.VERIFIED, full, (seconds, status))
                self.assertEqual(full, seconds >= gate_failures.MIN_TIMEOUT_S)
        self.assertEqual(gate_failures.MIN_TIMEOUT_S, 27)

    def test_doctor_says_when_a_timeout_is_too_small_to_record_a_failure(self):
        from diaktoros import doctor
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 3}}})
        check = self.doctor_lines()["gate:timeout:default"]
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn(f"at least {gate_failures.MIN_RECORDABLE_S:g}s", check.detail)
        self.assertIn("too small", check.detail)

    def test_a_held_ledger_lock_cannot_cost_the_record(self):
        # Arbiter's lock-held variant: 6s gateway, the gate hangs, and another process holds the
        # loop ledger's lock through the record phase. Before: rc -9, nothing recorded.
        import fcntl
        self.gateway_config({"platforms": {"webhook": {"script_timeout_seconds": 6}}})
        t.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(t.STATE_DIR / "gate-failures.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                proc, elapsed = gateway_run("gate_reviewer.py", t.pr_payload(7),
                                            {"DIAKTOROS_GH_STUB": str(self.hang_stub())},
                                            kill_after=6)
            except subprocess.TimeoutExpired:
                self.fail("killed at 6s with the ledger lock held, nothing recorded")
        self.assertEqual(proc.returncode, 3)
        (entry,) = gate_failures.fallback_ledger().entries().values()
        self.assertEqual(entry["kind"], "timeout")
        self.assertIn("busy", entry.get("recorded_here_because", ""))
        self.assertIn("recorded", proc.stderr)

    # -- a stopped gate leaves a record ---------------------------------------------------------

    def test_a_sigterm_leaves_a_record(self):
        import signal as sig
        script = t.TMP / "stopped_gate.py"
        script.write_text(
            f"import sys\nsys.path.insert(0, {str(t.ROOT)!r})\n"
            "from diaktoros import gate_failures\n"
            "def main():\n    import time\n    print('ready', file=sys.stderr, flush=True)\n"
            "    time.sleep(60)\n"
            "gate_failures.run('gate_reviewer', main)\n")
        proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=t.env())
        proc.stdin.write(json.dumps(t.pr_payload(7)))
        proc.stdin.close()
        proc.stderr.readline()                           # main() is running
        proc.send_signal(sig.SIGTERM)
        proc.wait(timeout=20)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(proc.returncode, 143)
        (entry,) = loop_entries().values()
        self.assertEqual((entry["kind"], entry["error_type"]), ("stopped", "GateStopped"))
        self.assertIn("SIGTERM", entry["error"])

    def test_naming_the_ledgers_never_escapes_the_failure_path(self):
        from unittest import mock
        with mock.patch.object(gate_failures, "_loop_for", side_effect=RuntimeError("bad loop")):
            self.assertEqual([l.path for l in gate_failures._ledgers_for({})],
                             [gate_failures.fallback_ledger().path])
        with mock.patch.object(gate_failures, "_loop_for", side_effect=RuntimeError("bad loop")), \
             mock.patch.object(gate_failures, "fallback_ledger", side_effect=OSError("no home")):
            self.assertEqual(gate_failures._ledgers_for({}), [])

    def claim_then(self, action: str):
        script = t.TMP / "claiming_gate.py"
        script.write_text(
            f"import sys\nsys.path.insert(0, {str(t.ROOT)!r})\n"
            "from diaktoros import config, gate_failures, state\n"
            "def main():\n"
            "    st = state.state_for(config.load_id('widgets'))\n"
            f"    st.acquire('reviewer', {t.REPO + '#7'!r}, {t.HEAD_A!r}, 'test claim')\n"
            f"    {action}\n"
            "gate_failures.run('gate_reviewer', main)\n")
        proc = subprocess.run([sys.executable, str(script)], input=json.dumps(t.pr_payload(7)),
                              capture_output=True, text=True, timeout=30,
                              env={**t.env(), "DIAKTOROS_GATE_BUDGET_S": "1"})
        return proc, state_mod.LoopState(config.load_id("widgets"))

    def test_a_timeout_after_claiming_a_seat_releases_it(self):
        proc, st = self.claim_then("import time; time.sleep(60)")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(st.live_locks("reviewer"), {})
        (entry,) = loop_entries().values()
        self.assertEqual(entry["released_claims"], [f"reviewer:{t.REPO}#7"])

    def test_a_crash_after_claiming_a_seat_releases_it(self):
        proc, st = self.claim_then("raise RuntimeError('boom')")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(st.live_locks("reviewer"), {})

    def test_a_claim_that_succeeded_is_kept(self):
        proc, st = self.claim_then("print('[SILENT]')")
        self.assertEqual(proc.returncode, 0)
        self.assertIn(f"{t.REPO}#7", st.live_locks("reviewer"))

    def test_real_gate_timeout_leaves_no_seat_or_inflight_mark(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        proc, _ = gateway_run("gate_fixer.py", t.review_payload(7),
                              {"DIAKTOROS_GH_STUB": str(hang), "DIAKTOROS_GATE_BUDGET_S": "1"})
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(t.load_state("locks.json"), {})
        self.assertEqual(t.load_state("inflight.json"), {})

    # -- the watchdog's own reads are budgeted -------------------------------------------------

    def test_hanging_github_cannot_stall_the_watchdog(self):
        hang = t.TMP / "hang_stub.py"
        hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        hang.chmod(0o755)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                              text=True, cwd=str(SCRIPTS), timeout=60,
                              env={**t.env(), "DIAKTOROS_GH_STUB": str(hang),
                                   "DIAKTOROS_WATCHDOG_BUDGET_S": "2"})
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(proc.returncode, 0)
        # One sweep that ran out of time is not yet an outage (slow reads spend it too): like
        # any failure short of a 401/403 it is said after READ_FAILURE_SWEEPS in a row.
        from scripts import watchdog as wd
        self.assertNotIn("watchdog stopped", proc.stdout)
        for _ in range(wd.READ_FAILURE_SWEEPS - 1):
            proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")],
                                  capture_output=True, text=True, cwd=str(SCRIPTS), timeout=60,
                                  env={**t.env(), "DIAKTOROS_GH_STUB": str(hang),
                                       "DIAKTOROS_WATCHDOG_BUDGET_S": "2"})
        (line,) = [x for x in proc.stdout.splitlines() if "watchdog stopped" in x]
        self.assertIn("the sweep ran out of its 2s budget", line)
        self.assertIn("reads were slow or did not answer", line)
        self.assertNotIn("no HTTP answer", line)

    # -- the real watchdog in its normal (non-test) mode -------------------------------------

    def switch_stub(self, default: str = "world", paths: dict | None = None) -> pathlib.Path:
        """A stub in front of the fixture's GitHub: hang, answer a status, or serve the world."""
        mode = t.TMP / "gh_mode.json"
        mode.write_text(json.dumps({"default": default, "paths": paths or {}}))
        stub = t.TMP / "switch_stub.py"
        if not stub.exists():
            stub.write_text(
                f"#!{sys.executable}\nimport json, os, sys, time\n"
                f"with open({str(mode)!r}) as f:\n    mode = json.load(f)\npath = sys.argv[1]\n"
                "status = next((v for k, v in mode['paths'].items() if path.endswith(k)),"
                " mode['default'])\n"
                "if status == 'hang':\n    time.sleep(60)\n"
                "elif status != 'world':\n"
                "    print(json.dumps({'__gh_stub_response__': {'status': int(status),"
                " 'body': {'message': 'stubbed'}}}))\n"
                f"else:\n    os.execv({str(t.STUB)!r}, [{str(t.STUB)!r}, *sys.argv[1:]])\n")
            stub.chmod(0o755)
        return stub

    def normal_watchdog(self, stub: pathlib.Path, budget: str = "600"):
        """The watchdog exactly as cron runs it — no DIAKTOROS_TEST — against the stub."""
        env = {k: v for k, v in t.env().items() if k != "DIAKTOROS_TEST"}
        env.update(DIAKTOROS_GH_STUB=str(stub), DIAKTOROS_WATCHDOG_BUDGET_S=budget)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                              text=True, cwd=str(SCRIPTS), timeout=120, env=env)
        return proc, time.monotonic() - started

    def test_normal_mode_hanging_github_stops_once_then_health_alerts_and_recovers(self):
        t.set_prs({"7": t.pr(7)})
        hang = self.switch_stub("hang")
        first, took = self.normal_watchdog(hang, "5")
        self.assertEqual(first.returncode, 0)
        self.assertLess(took, 15)
        self.assertNotIn("Traceback", first.stdout + first.stderr)
        self.assertEqual(first.stdout.strip(), "")          # one ran-out sweep: not yet said
        second, _ = self.normal_watchdog(hang, "5")
        self.assertEqual(second.stdout.strip(), "")
        third, _ = self.normal_watchdog(hang, "5")
        stopped = [line for line in third.stdout.splitlines() if "watchdog stopped" in line]
        self.assertEqual(len(stopped), 1, third.stdout)
        self.assertIn(f"reading as {config.load_id('widgets')['read_token']}", stopped[0])
        self.assertIn("(3 sweep(s) in a row since", stopped[0])
        again, _ = self.normal_watchdog(hang, "5")          # still hanging, inside the cooldown
        self.assertEqual((again.returncode, again.stdout.strip()), (0, ""))
        watch = t.load_state("watchdog.json")["github_read"]
        self.assertEqual((watch["sweeps"], watch["status"]), (4, None))
        dead, _ = self.normal_watchdog(self.switch_stub("401"), "5")
        alert = [line for line in dead.stdout.splitlines() if "cannot read GitHub" in line]
        self.assertEqual(len(alert), 1, dead.stdout)
        self.assertIn("HTTP 401", alert[0])
        self.assertIn("(5 sweep(s) since", alert[0])
        well, _ = self.normal_watchdog(self.switch_stub("world"))
        self.assertIn("GitHub reads work again", well.stdout)

    def test_normal_mode_owned_and_unowned_failed_reads_are_each_said_once(self):
        t.set_prs({"7": t.pr(7, requested=t.SEAT), "8": t.pr(8, requested=t.SEAT)})
        stub = self.switch_stub("world", {"/pulls/7": 502, "/pulls/8": 410})
        env = {"DIAKTOROS_GH_STUB": str(stub)}
        gateway_run("gate_reviewer.py", t.pr_payload(7), env)   # owned by gate-failures.json
        gateway_run("gate_reviewer.py", t.pr_payload(8), env)   # 410: an answer, #54 reports it
        (key, entry), = loop_entries().items()
        self.assertEqual((entry["pr"], entry["kind"]), (7, "incomplete"))
        out, _ = self.normal_watchdog(self.switch_stub("world"))
        lines = out.stdout.splitlines()
        owned = [line for line in lines if "/pulls/7" in line or "#7 " in line]
        unowned = [line for line in lines if "/pulls/8" in line or "#8 " in line]
        self.assertEqual(len(owned), 1, out.stdout)
        self.assertIn(f"gate failure {key}", owned[0])
        self.assertEqual(len(unowned), 1, out.stdout)
        self.assertIn("HTTP 410", unowned[0])
        self.assertNotIn("Traceback", out.stdout + out.stderr)

    def test_explain_only_promises_a_retry_it_can_make(self):
        ledger = self.redrivable_entry()
        loop = config.load_id("widgets")
        self.assertIn("the next sweep retries it", gate.gate_failure_line(ledger.open_for(7)[0]))
        from unittest import mock
        with mock.patch.object(gate_failures, "SCRIPTS_DIR", t.TMP / "no-scripts-here"):
            line = gate.gate_failure_line(gate_failures.open_for(loop, 7)[0])
        self.assertNotIn("retries it", line)
        self.assertIn("gate_reviewer.py not found", line)

    def test_a_failure_with_no_pr_is_shown_by_explain(self):
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.record("np", {"gate": "gate_reviewer", "kind": "crash", "pr": None,
                             "redrivable": True, "error_type": "X", "error": "y"}, "{}")
        shown = [gate.gate_failure_line(e) for e in
                 gate_failures.open_for(config.load_id("widgets"), 7)]
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("gate failure np", shown[0])
        self.assertIn("names no PR", shown[0])

    def test_a_paused_loop_still_reports_its_gate_failures(self):
        from unittest import mock
        from scripts import watchdog as wd
        ledger = self.redrivable_entry()
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gate, "hooks_read", return_value=(False, "paused")), \
             mock.patch.object(wd, "SCRIPTS", self.slow_gate_dir(0)[0]):
            lines = wd.sweep_loop(config.load_id("widgets"), state_mod.state_for(
                config.load_id("widgets")))
        said = [x for x in lines if "gate failure k1" in x]
        self.assertEqual(len(said), 1, lines)
        self.assertIn("hooks are paused", said[0])
        self.assertEqual(ledger.entries()["k1"]["redrives"], 0)      # paused: never re-driven

    def test_a_scoped_watchdog_also_sweeps_the_no_loop_ledger(self):
        proc, _ = gateway_run("gate_reviewer.py", {"repository": "acme/widgets", "action": "x"})
        self.assertEqual(proc.returncode, 2)
        out = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py"), "--loop", "widgets"],
                             capture_output=True, text=True, cwd=str(SCRIPTS), timeout=120,
                             env=t.env()).stdout
        self.assertIn("(no loop) — gate failure", out)

    def test_an_alert_is_committed_only_after_it_is_said(self):
        ledger = self.redrivable_entry()
        ledger.update("k1", {"redrivable": False})
        said = []

        def killed(line):                       # the sweep dies while saying it
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=killed)
        self.assertNotIn("alerted_at", ledger.entries()["k1"])
        # Its claim is leased: once the lease is out, the next sweep says it — and only then
        # is it marked alerted, so the cooldown holds after that.
        ledger.update("k1", {"claim": {"sweep": "dead", "until": time.time() - 1}})
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        self.assertEqual(len(said), 1)
        self.assertIn("alerted_at", ledger.entries()["k1"])
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        self.assertEqual(len(said), 1)

    def test_the_corrupt_copy_pointer_is_re_raised_on_the_cooldown(self):
        ledger = self.corrupt_ledger()
        ledger.snapshot()
        said = []
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        self.assertEqual(len(said), 1)
        (key,) = [k for k, e in ledger.entries().items() if e.get("kind") == "ledger"]
        ledger.update(key, {"alerted_at": time.time() - 7200})
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        self.assertEqual(len(said), 2)

    # -- one event, one ledger (Arbiter on faf21e9) ---------------------------------------------

    def fault_gate(self, patch: str) -> subprocess.CompletedProcess:
        """A gate that crashes, with ``patch`` applied inside its process first."""
        script = t.TMP / "fault_gate.py"
        script.write_text(
            f"import sys\nsys.path.insert(0, {str(t.ROOT)!r})\n"
            "from diaktoros import gate_failures, state\n"
            f"{patch}\n"
            "def main():\n    raise RuntimeError('boom')\n"
            "gate_failures.run('gate_reviewer', main)\n")
        return subprocess.run([sys.executable, str(script)], input=json.dumps(t.pr_payload(7)),
                              capture_output=True, text=True, timeout=30, env=t.env())

    def both_ledgers(self) -> tuple[dict, dict]:
        return (gate_failures.Ledger(t.STATE_DIR).entries(),
                gate_failures.fallback_ledger().entries())

    def test_a_write_that_landed_then_raised_is_not_recorded_twice(self):
        # The directory fsync after os.replace fails: the entry is already published.
        proc = self.fault_gate(
            "import os, stat\nreal = os.fsync\n"
            "def fsync(fd):\n"
            "    if stat.S_ISDIR(os.fstat(fd).st_mode):\n"
            "        raise OSError(5, 'Input/output error (injected)')\n"
            "    return real(fd)\n"
            "state.os.fsync = fsync\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        loop, fallback = self.both_ledgers()
        self.assertEqual(len(loop), 1, proc.stderr)
        self.assertEqual(fallback, {}, proc.stderr)
        self.assertIn(str(gate_failures.Ledger(t.STATE_DIR).path), proc.stderr)

    def test_a_record_cut_off_by_the_record_alarm_after_landing_is_not_recorded_twice(self):
        proc = self.fault_gate(
            "import time\nreal = gate_failures.Ledger._save\n"
            "def slow(self, data):\n    real(self, data)\n    time.sleep(gate_failures.RECORD_S + 1)\n"
            "gate_failures.Ledger._save = slow\n")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        loop, fallback = self.both_ledgers()
        self.assertEqual(len(loop), 1, proc.stderr)
        self.assertEqual(fallback, {}, proc.stderr)

    def test_one_event_in_both_ledgers_is_alerted_and_redriven_by_one(self):
        loop_ledger = self.redrivable_entry()
        fallback = gate_failures.fallback_ledger()
        fallback.record("k1", {"gate": "gate_reviewer", "kind": "crash", "pr": 7, "repo": t.REPO,
                               "redrivable": True, "error_type": "X", "error": "y"},
                        json.dumps(t.pr_payload(7)))
        scripts, launches = self.slow_gate_dir(0)
        said = gate_failures.sweep(fallback, "(no loop)", scripts, cooldown_s=3600)
        self.assertEqual(said, [])
        self.assertTrue(fallback.entries()["k1"]["resolved"])
        self.assertIn(str(loop_ledger.path), fallback.entries()["k1"]["resolution"])
        for _ in range(gate_failures.MAX_REDRIVES + 2):
            gate_failures.sweep(loop_ledger, "[w]", scripts, cooldown_s=0)
            gate_failures.sweep(fallback, "(no loop)", scripts, cooldown_s=0)
        self.assertEqual(len(launches.read_text().split()), gate_failures.MAX_REDRIVES)

    def test_a_failure_held_by_the_fallback_ledger_is_shown_and_owned_there(self):
        import fcntl
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        t.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(t.STATE_DIR / "gate-failures.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)            # the loop ledger is busy
            gateway_run("gate_reviewer.py", t.pr_payload(7), {"DIAKTOROS_GH_STUB": str(failing)})
        (key, _), = gate_failures.fallback_ledger().entries().items()
        loop = config.load_id("widgets")
        shown = gate_failures.open_for(loop, 7)
        self.assertEqual([e["id"] for e in shown], [key])
        self.assertIn(str(gate_failures.fallback_ledger().path), gate.gate_failure_line(shown[0]))
        failure = state_mod.LoopState(loop).github_failure()
        self.assertEqual(failure.get("owned_by"), f"gate-failures:{key}")
        self.assertEqual(failure.get("owned_in"), str(gate_failures.fallback_ledger().path))
        local = gate._explain_state(loop, state_mod.LoopState(loop), f"{t.REPO}#7", 7, "", time.time())
        self.assertIn(str(gate_failures.fallback_ledger().path), local["github"])

    def test_the_alert_marks_the_attempt_it_said_not_a_later_one(self):
        ledger = self.redrivable_entry()
        ledger.update("k1", {"redrivable": False})
        said = []

        def say(line):                     # another delivery lands while the line goes out
            said.append(line)
            ledger.record("k1", {"gate": "gate_reviewer", "kind": "crash", "pr": 7,
                                 "redrivable": False, "error_type": "X", "error": "y"}, "{}")
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=say)
        entry = ledger.entries()["k1"]
        self.assertEqual((entry["attempts"], entry["alerted_attempts"]), (2, 1))
        gate_failures.sweep(ledger, "[w]", SCRIPTS, cooldown_s=3600, emit=said.append)
        self.assertEqual(len(said), 2)                 # the second delivery is said too
        self.assertIn("2 attempt(s)", said[1])

    def test_a_resolved_entry_gives_up_its_read(self):
        from unittest import mock
        from scripts import watchdog as wd
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        gateway_run("gate_reviewer.py", t.pr_payload(7), {"DIAKTOROS_GH_STUB": str(failing)})
        (key, _), = loop_entries().items()
        loop = config.load_id("widgets")
        st = state_mod.LoopState(loop)
        self.assertEqual(st.github_failure().get("owned_by"), f"gate-failures:{key}")
        gateway_run("gate_reviewer.py", t.pr_payload(7))           # redelivered: completes
        self.assertTrue(loop_entries()[key]["resolved"])
        failure = st.github_failure()
        self.assertNotIn("owned_by", failure)
        self.assertIn(key, failure.get("resolved_by", ""))
        local = gate._explain_state(loop, st, f"{t.REPO}#7", 7, "", time.time())
        self.assertNotIn("tracked as", local["github"])
        self.assertIn("resolved", local["github"])
        ok = wd.gh.Response({"login": t.REVIEWER}, "", 200, {})
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gh, "auth_probe", return_value=ok):
            lines = wd.github_health(loop, st, {}, time.time(), True, "")
        self.assertEqual([x for x in lines if "could not" in x], [])   # settled, not re-announced

    def test_resolve_survives_a_junk_resolved_at_and_keeps_it(self):
        # #167: retention aged every resolved entry by `now - (resolved_at or 0)`, so a junk
        # `resolved_at` (a hand edit, an older-shape write) raised TypeError out of the whole
        # resolve — the sweep-aborting class #80 closes. The age is now read with the same rule
        # mark_at applies to `at`: only a finite number is a time, and junk is kept, never
        # retired on a guess.
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        ledger.path.write_text(json.dumps({
            "junk_at":    {"resolved": True, "resolution": "old", "resolved_at": "not-a-number"},
            "past":       {"resolved": True, "resolution": "old",
                           "resolved_at": now - gate_failures.RESOLVED_RETENTION_S - 60},
            "fresh":      {"resolved": True, "resolution": "new", "resolved_at": now},
            # #175 review: an int outside float range — math.isfinite(10**400) raised OverflowError
            # and aborted resolve, breaking this test's own stated guarantee. Kept, never raised on.
            "huge_int":   {"resolved": True, "resolution": "old", "resolved_at": 10 ** 400},
            "neg_huge":   {"resolved": True, "resolution": "old", "resolved_at": -(10 ** 400)},
            "target":     {"gate": "gate_reviewer", "pr": 7, "attempts": 1},
        }))
        self.assertTrue(ledger.resolve("target", "operator closed it", settle=False))
        data = json.loads(ledger.path.read_text())
        self.assertTrue(data["target"]["resolved"])                    # the resolve landed
        self.assertNotIn("past", data)                                 # past retention: retired
        self.assertIn("fresh", data)                                   # inside retention: kept
        self.assertIn("junk_at", data)                                 # unparseable: kept, not guessed
        self.assertIn("huge_int", data)                                # out of float range: kept
        self.assertIn("neg_huge", data)                                # and its negative twin

    def test_an_owned_read_whose_entry_was_moved_aside_is_not_promised(self):
        # Arbiter's repro: a 502 failure owns its read; the ledger is torn and moved aside (which
        # discards the entry); the same event then completes cleanly.
        from unittest import mock
        from scripts import watchdog as wd
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        gateway_run("gate_reviewer.py", t.pr_payload(7), {"DIAKTOROS_GH_STUB": str(failing)})
        (key, _), = loop_entries().items()
        loop = config.load_id("widgets")
        st = state_mod.LoopState(loop)
        self.assertEqual(st.github_failure().get("owned_by"), f"gate-failures:{key}")
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.path.write_text("{torn")
        ledger.snapshot()                                          # a writer moves it aside
        self.assertNotIn(key, ledger.entries())
        # Read-only explain, before anything settles it: no promise of a line that cannot come.
        local = gate._explain_state(loop, st, f"{t.REPO}#7", 7, "", time.time())
        self.assertNotIn("its gate-failure line says what happens next", local["github"])
        self.assertIn("no longer in any gate-failure ledger", local["github"])
        # The health check does not keep suppressing it on a marker nothing backs.
        ok = wd.gh.Response({"login": t.REVIEWER}, "", 200, {})
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gh, "auth_probe", return_value=ok):
            lines = wd.github_health(loop, st, {}, time.time(), True, "")
        self.assertEqual(len([x for x in lines if "could not GET" in x]), 1, lines)
        # And a clean completion of the same event settles it, although no ledger held it.
        gateway_run("gate_reviewer.py", t.pr_payload(7))
        failure = st.github_failure()
        self.assertNotIn("owned_by", failure)
        self.assertIn(key, failure.get("resolved_by", ""))

    def test_resolving_a_duplicate_does_not_settle_the_open_original(self):
        loop_ledger = self.redrivable_entry()
        st = state_mod.LoopState(config.load_id("widgets"))
        st.github_failure_record({"at": time.time(), "where": "gate_reviewer.py", "method": "GET",
                                  "path": "/x", "error": "HTTP 502", "status": 502,
                                  "owned_by": "gate-failures:k1", "owned_in": str(loop_ledger.path)})
        fallback = gate_failures.fallback_ledger()
        fallback.record("k1", {"gate": "gate_reviewer", "kind": "crash", "pr": 7, "repo": t.REPO,
                               "redrivable": True, "error_type": "X", "error": "y"}, "{}")
        gate_failures.sweep(fallback, "(no loop)", SCRIPTS, cooldown_s=3600)
        self.assertTrue(fallback.entries()["k1"]["resolved"])
        self.assertEqual(st.github_failure().get("owned_by"), "gate-failures:k1")   # still open

    # -- owner_state looks at every ledger; an immovable ledger never silences a read -------

    def owned_502(self) -> tuple[str, dict]:
        failing = t.TMP / "fail_stub.py"
        failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('HTTP 502')\nsys.exit(1)\n")
        failing.chmod(0o755)
        t.set_prs({"7": t.pr(7, requested=t.SEAT)})
        gateway_run("gate_reviewer.py", t.pr_payload(7), {"DIAKTOROS_GH_STUB": str(failing)})
        (key, _), = loop_entries().items()
        return key, config.load_id("widgets")

    def health_lines(self, loop) -> list[str]:
        from unittest import mock
        from scripts import watchdog as wd
        ok = wd.gh.Response({"login": t.REVIEWER}, "", 200, {})
        with mock.patch.object(wd, "TEST", False), \
             mock.patch.object(wd.gh, "auth_probe", return_value=ok):
            return wd.github_health(loop, state_mod.LoopState(loop), {}, time.time(), True, "")

    def test_an_owner_open_in_any_ledger_is_open(self):
        # Arbiter: the same key in both ledgers, owned_in naming the no-loop one; its copy resolved
        # as a duplicate while the loop's copy is open — and later pruned by retention.
        key, loop = self.owned_502()
        st = state_mod.LoopState(loop)
        fallback = gate_failures.fallback_ledger()
        fallback.record(key, {"gate": "gate_reviewer", "kind": "incomplete", "pr": 7,
                              "repo": t.REPO, "redrivable": True, "error_type": "X",
                              "error": "y"}, "{}")
        st.github_failure_record({**st.github_failure(), "owned_in": str(fallback.path)})
        gate_failures.sweep(fallback, "(no loop)", SCRIPTS, cooldown_s=3600)   # duplicate
        self.assertTrue(fallback.entries()[key]["resolved"])
        for stage in ("duplicate resolved", "duplicate pruned"):
            with self.subTest(stage=stage):
                self.assertEqual(gate_failures.owner_state(loop, st.github_failure()), "open")
                local = gate._explain_state(loop, st, f"{t.REPO}#7", 7, "", time.time())
                self.assertIn("its gate-failure line says what happens next", local["github"])
                self.assertEqual([x for x in self.health_lines(loop) if "could not GET" in x], [])
            data = json.loads(fallback.path.read_text())
            data.pop(key, None)                            # the 7-day retention prune
            fallback.path.write_text(json.dumps(data))

    def test_a_settled_duplicate_never_stands_in_for_the_owner(self):
        # Arbiter on 2b1b47b: the owner's ledger is lost (moved aside, unreadable, pruned) while the
        # no-loop ledger holds the same key resolved *as a duplicate* — which never owned the read.
        for shape in ("moved aside", "unreadable", "pruned"):
            with self.subTest(shape=shape):
                t.reset(prs={})
                for old in t.STATE_DIR.glob(gate_failures.LEDGER + "*"):
                    old.rmdir() if old.is_dir() else old.unlink()
                key, loop = self.owned_502()
                st = state_mod.LoopState(loop)
                fallback = gate_failures.fallback_ledger()
                fallback.record(key, {"gate": "gate_reviewer", "kind": "incomplete", "pr": 7,
                                      "repo": t.REPO, "redrivable": True, "error_type": "X",
                                      "error": "y"}, "{}")
                gate_failures.sweep(fallback, "(no loop)", SCRIPTS, cooldown_s=3600)
                self.assertTrue(fallback.entries()[key]["resolved"])
                ledger = gate_failures.Ledger(t.STATE_DIR)
                if shape == "moved aside":
                    ledger.path.write_text("{torn")
                    ledger.snapshot()
                elif shape == "unreadable":
                    self.ledger_as_directory()
                else:
                    data = json.loads(ledger.path.read_text())
                    data.pop(key)
                    ledger.path.write_text(json.dumps(data))
                state = gate_failures.owner_state(loop, st.github_failure())
                self.assertNotEqual(state, "resolved")
                local = gate._explain_state(loop, st, f"{t.REPO}#7", 7, "", time.time())
                self.assertNotIn("which has resolved", local["github"])
                out, _ = self.normal_watchdog(self.switch_stub("world"))
                self.assertIn("could not GET /repos/acme/widgets/pulls/7", out.stdout, out.stdout)
                gate_failures.fallback_ledger().path.unlink(missing_ok=True)
                if ledger.path.is_dir():
                    ledger.path.rmdir()

    def ledger_as_directory(self) -> gate_failures.Ledger:
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.path.unlink(missing_ok=True)
        ledger.path.mkdir()                                # unreadable as JSON and as bytes
        self.addCleanup(lambda: ledger.path.rmdir() if ledger.path.is_dir() else None)
        return ledger

    def test_a_ledger_that_cannot_be_moved_aside_never_silences_its_read(self):
        key, loop = self.owned_502()
        self.ledger_as_directory()
        st = state_mod.LoopState(loop)
        self.assertEqual(gate_failures.owner_state(loop, st.github_failure()), "unreadable")
        local = gate._explain_state(loop, st, f"{t.REPO}#7", 7, "", time.time())
        self.assertNotIn("its gate-failure line says what happens next", local["github"])
        self.assertIn("cannot be read", local["github"])
        # A real watchdog in normal mode names the read itself, not only the ledger.
        out, _ = self.normal_watchdog(self.switch_stub("world"))
        self.assertIn("/repos/acme/widgets/pulls/7", out.stdout)
        self.assertIn("could not be copied aside", out.stdout)

    def test_a_failure_building_the_ledger_paths_is_unreadable_not_gone(self):
        key, loop = self.owned_502()
        st = state_mod.LoopState(loop)
        failure = dict(st.github_failure(), owned_by="gate-failures:no-such-key")
        # No ledger holds this key, so only the broken path build separates gone from unreadable.
        self.assertEqual(gate_failures.owner_state(loop, failure), "gone")
        with mock.patch.object(gate_failures, "fallback_ledger", side_effect=OSError("boom")):
            self.assertEqual(gate_failures.owner_state(loop, failure), "unreadable")

    def test_explain_promises_a_move_aside_only_when_one_can_happen(self):
        loop = config.load_id("widgets")
        ledger = gate_failures.Ledger(t.STATE_DIR)
        ledger.dir.mkdir(parents=True, exist_ok=True)
        ledger.path.write_text("{torn")
        (line,) = [gate.gate_failure_line(e) for e in gate_failures.open_for(loop, 7)
                   if e.get("kind") == "ledger"]
        self.assertIn("moves it aside", line)
        ledger.path.unlink()
        self.ledger_as_directory()
        (line,) = [gate.gate_failure_line(e) for e in gate_failures.open_for(loop, 7)
                   if e.get("kind") == "ledger"]
        self.assertNotIn("moves it aside", line)
        self.assertIn("cannot be moved aside automatically", line)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file anyway")
    def test_a_mode_000_ledger_is_still_moved_aside(self):
        key, loop = self.owned_502()
        ledger = gate_failures.Ledger(t.STATE_DIR)
        before = ledger.path.read_bytes()
        ledger.path.chmod(0)
        self.addCleanup(lambda: [c.chmod(0o600) for c in self.corrupt_copies()])
        ledger.snapshot()                                   # a writer: link, then fresh ledger
        (copy,) = self.corrupt_copies()
        copy.chmod(0o600)
        self.assertEqual(copy.read_bytes(), before)
        st = state_mod.LoopState(loop)
        self.assertEqual(gate_failures.owner_state(loop, st.github_failure()), "gone")

    # -- per-PR review reads are visible, and "reads work again" means all of them (#99) ------

    REVIEWS_7 = "/pulls/7/reviews?per_page=100"

    def test_a_failed_per_pr_review_read_is_said_and_feeds_the_health_alert(self):
        t.set_prs({"7": t.pr(7)})
        self.normal_watchdog(self.switch_stub("world"))             # arms the loop
        outs = []
        for _ in range(gate_failures.MAX_REDRIVES):                  # three failing sweeps
            proc, _ = self.normal_watchdog(self.switch_stub("world", {self.REVIEWS_7: 502}))
            outs.append(proc.stdout)
        per_pr = [[x for x in out.splitlines() if "could not read reviews" in x] for out in outs]
        alert = [[x for x in out.splitlines() if "cannot read GitHub" in x] for out in outs]
        # Sweeps 1-2: the one bounded per-PR line, with the reason, no verdict guessed.
        for lines in per_pr[:2]:
            self.assertEqual(len(lines), 1, outs)
            self.assertIn("#7: review page 1: HTTP 502", lines[0])
            self.assertIn("stall check skipped", lines[0])
            self.assertNotIn("\n", lines[0])
        # Sweep 3: a 5xx pattern escalates to #99's health alert, and is not said twice.
        self.assertEqual(alert[:2], [[], []])
        self.assertEqual(len(alert[2]), 1, outs[2])
        self.assertIn("PR review read: #7", alert[2][0])
        self.assertEqual(per_pr[2], [])
        self.assertEqual(t.load_state("watchdog.json")["github_read"]["sweeps"], 3)
        well, _ = self.normal_watchdog(self.switch_stub("world"))
        self.assertIn("GitHub reads work again", well.stdout)

    def test_every_pr_whose_reviews_failed_is_named_when_the_alert_fires(self):
        t.set_prs({"7": t.pr(7), "8": t.pr(8)})
        self.normal_watchdog(self.switch_stub("world"))              # arms the loop
        stub = self.switch_stub("world", {self.REVIEWS_7: 401,
                                          "/pulls/8/reviews?per_page=100": 404})
        proc, _ = self.normal_watchdog(stub)
        out = proc.stdout
        self.assertIn("cannot read GitHub", out)
        self.assertIn("#7", out)
        self.assertIn("#8: review page 1: HTTP 404", out)

    def test_reads_work_again_waits_for_every_read_in_the_sweep(self):
        t.set_prs({"7": t.pr(7)})
        self.normal_watchdog(self.switch_stub("world"))              # arms the loop
        dead, _ = self.normal_watchdog(self.switch_stub("401"))
        self.assertIn("cannot read GitHub", dead.stdout)
        # /user, the hooks and the listing answer again, but #7's reviews do not.
        partial, _ = self.normal_watchdog(self.switch_stub("world", {self.REVIEWS_7: 404}))
        self.assertNotIn("reads work again", partial.stdout)
        self.assertIn("could not read reviews for 1 PR(s)", partial.stdout)
        well, _ = self.normal_watchdog(self.switch_stub("world"))
        self.assertIn("GitHub reads work again", well.stdout)

    def test_a_gate_drain_runs_on_the_gates_clock(self):
        from unittest import mock
        from diaktoros import gh
        loop = config.load_id("widgets")
        with mock.patch.object(gate.subprocess, "run") as run:
            gh.begin_gate(time.monotonic() + 1.5)
            try:
                gate.drain_seat(loop, "reviewer")
            finally:
                gh.end_gate()
            run.assert_not_called()                       # deferred to the watchdog
            gh.begin_gate(time.monotonic() + 10)
            try:
                gate.drain_seat(loop, "reviewer")
            finally:
                gh.end_gate()
        timeout = run.call_args.kwargs["timeout"]
        self.assertTrue(4 < timeout <= 5, timeout)
        self.assertLess(float(run.call_args.kwargs["env"]["DIAKTOROS_WATCHDOG_BUDGET_S"]), 5)

    def test_secrets_are_scrubbed_and_text_bounded(self):
        text = gate_failures._bounded("x" * 50 + " ghp_" + "A" * 36, 40)
        self.assertNotIn("ghp_", text)
        self.assertLessEqual(len(text), 40)


if __name__ == "__main__":
    unittest.main()
