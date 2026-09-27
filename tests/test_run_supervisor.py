"""Fixture-only lifecycle tests: never invoke a real Hermes agent."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from unittest.mock import patch

from review_loop import broker, broker_ipc, config, gate, run_supervisor, seat_model, trusted_turn
from review_loop.hostdirs import HostStateGone
from review_loop.run_supervisor import (_WORKERS, MAX_ATTEMPTS, SILENT, LedgerMissing,
                                        Supervisor)

# Longest a test waits for its detached workers after it ends. The slowest fixture child
# sleeps 2s under a 5s child timeout; this leaves room for a loaded runner.
WORKER_EXIT_TIMEOUT = 20


def _processes_naming(marker: str) -> dict[int, str]:
    """Live processes (other than this one) whose command line names ``marker``."""
    found = {}
    proc = Path('/proc')
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmd = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
            except OSError:
                continue
            if marker in cmd:  # a zombie's command line is empty: it is not writing
                found[int(entry.name)] = cmd.strip()
        return found
    out = subprocess.run(['ps', '-axww', '-o', 'pid=,command='], capture_output=True,
                         text=True, check=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(' ')
        if pid.isdigit() and int(pid) != os.getpid() and marker in cmd:
            found[int(pid)] = cmd.strip()
    return found


def wait_for_workers(root: Path, timeout: float = WORKER_EXIT_TIMEOUT) -> None:
    """Block until no detached worker or fixture child still uses ``root`` (#108).

    ``enqueue`` returns as soon as it has spawned a detached worker, and a finished worker's
    ``recover`` can spawn another, which is not this process's child. A test that returns on
    the ledger reaching a terminal state can therefore tear down its temp dir while a worker
    still opens the ledger there (or recreates the dir). Every worker and child names a path
    under ``root`` on its command line, and only a live worker spawns another, so an empty
    scan means none is left.
    """
    marker = str(root)
    until = time.monotonic() + timeout
    while True:
        for worker in list(_WORKERS):
            worker.poll()  # reap our own exited children so they do not linger as zombies
        live = _processes_naming(marker)
        if not live:
            return
        if time.monotonic() >= until:
            for pid in live:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            raise AssertionError(f"detached workers still running {timeout}s after the test "
                                 f"under {marker} (killed): {live}")
        time.sleep(0.05)


def own_lines(stream: io.StringIO) -> list[str]:
    """The review-loop's own stderr lines. Python 3.13 also reports, whenever the collector
    happens to run, ResourceWarnings for sqlite connections other tests left open."""
    return [line for line in stream.getvalue().splitlines() if line.startswith("review-loop")]


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Cleanups run last-in first-out: every worker is gone before the temp dir is removed.
        self.addCleanup(wait_for_workers, self.root)
        self.db = self.root / "ledger.sqlite"
        self.events = self.root / "launches"
        self.child = self.root / "child.py"
        self.child.write_text("import sys,time\nfrom pathlib import Path\n"
                              "with Path(sys.argv[1]).open('a') as f: f.write('launched\\n')\n"
                              "time.sleep(float(sys.argv[2]))\nsys.exit(int(sys.argv[3]))\n")
        # A child that runs until the test releases it: "still running" is then a fact the
        # test controls, not a race between a sleep and a loaded scheduler. It gives up
        # (rc 3) after a minute so a failed test never strands it.
        self.release_file = self.root / "release"
        self.gate = self.root / "gate.py"
        self.gate.write_text("import sys,time\nfrom pathlib import Path\n"
                             "with Path(sys.argv[1]).open('a') as f: f.write('launched\\n')\n"
                             "until = time.monotonic() + 60\n"
                             "while not Path(sys.argv[2]).exists():\n"
                             "    if time.monotonic() > until: sys.exit(3)\n"
                             "    time.sleep(0.02)\n")
        # Runs before wait_for_workers (last in, first out): a gated child always ends.
        self.addCleanup(self.release)

    def supervisor(self, delay=0.05, rc=0, **kw):
        return Supervisor(self.db, fixture_mode=True,
                          fixture_command=[sys.executable, str(self.child),
                                           str(self.events), str(delay), str(rc)], **kw)

    def gated_supervisor(self, **kw):
        return Supervisor(self.db, fixture_mode=True,
                          fixture_command=[sys.executable, str(self.gate),
                                           str(self.events), str(self.release_file)], **kw)

    def release(self):
        self.release_file.touch()

    def wait(self, sup, delivery, state, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            row = sup.get(delivery)
            if row and row["state"] == state:
                return row
            time.sleep(0.02)
        self.fail(f"{delivery} did not reach {state}: {sup.get(delivery)}")

    def launches(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def test_production_blocks_without_agent_fallback(self):
        sup = Supervisor(self.db)
        self.assertEqual(sup.enqueue("d", "o/r", 1, "sha", "reviewer"), SILENT)
        self.assertEqual(sup.get("d")["state"], "blocked")
        self.assertEqual(sup.recover(), SILENT)
        self.assertFalse(self.events.exists())
        with self.assertRaises(ValueError):
            Supervisor(self.db, fixture_command=["hermes", "chat"])

    def test_parallel_retries_only_one_launch_and_conflict_fails(self):
        sup = self.supervisor()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: sup.enqueue("d", "o/r", 1, "sha", "reviewer"), range(32)))
        self.assertEqual(results, [SILENT] * len(results))
        self.wait(sup, "d", "succeeded")
        sup.enqueue("other-delivery", "o/r", 1, "sha", "reviewer")
        sup.recover()
        # Every worker those calls could have started has exited: the count is final.
        wait_for_workers(self.root)
        self.assertEqual(len(self.launches()), 1)
        with self.assertRaises(ValueError):
            sup.enqueue("d", "o/r", 2, "sha", "reviewer")

    def test_detached_seat_queue_and_release(self):
        # "a" holds the only reviewer seat until released, so "b" must queue behind it.
        sup = self.gated_supervisor()
        started = time.monotonic()
        self.assertEqual(sup.enqueue("a", "o/r", 1, "a", "reviewer"), SILENT)
        # Detached: enqueue does not wait on a child that runs until released (up to 60s).
        self.assertLess(time.monotonic() - started, 5)
        self.wait(sup, "a", "running")
        sup.enqueue("b", "o/r", 2, "b", "reviewer")
        self.assertEqual(sup.get("b")["state"], "pending")
        self.assertEqual(sup.get("a")["state"], "running")
        self.release()
        self.wait(sup, "a", "succeeded")
        self.wait(sup, "b", "succeeded")
        self.assertEqual(len(self.launches()), 2)

    def test_worker_never_recreates_a_deleted_ledger_dir(self):
        # Hardening: a detached worker must never create host state. If the ledger's
        # directory is gone when a worker (spawned exactly as recover() spawns one) starts,
        # it exits cleanly and leaves the dir deleted rather than recreate an empty ledger.
        config = self.root / "runtime.json"
        config.write_text("{}")
        config.chmod(0o600)
        hermes = self.root / "hermes-home"
        hermes.mkdir()
        modes = {
            "fixture": lambda db: Supervisor(db, fixture_mode=True, fixture_command=[
                sys.executable, str(self.child), str(self.events), "0", "0"]),
            "production": lambda db: Supervisor(db, production_config=config, hermes_home=hermes),
        }
        for mode, make in modes.items():
            with self.subTest(mode=mode):
                state = self.root / f"{mode}-state"
                sup = make(state / "runs.sqlite")
                self.assertTrue(state.is_dir())
                shutil.rmtree(state)
                sup._spawn()
                worker = _WORKERS[-1]
                self.assertEqual(worker.wait(timeout=WORKER_EXIT_TIMEOUT), 0)
                self.assertFalse(state.exists(), f"{mode} worker recreated {state}")
        self.assertFalse(self.events.exists())

    def test_worker_mode_ledger_removed_mid_life_is_never_recreated(self):
        state = self.root / "late-state"
        Supervisor(state / "runs.sqlite")  # the host creates it
        worker = Supervisor(state / "runs.sqlite", create=False)
        shutil.rmtree(state)
        with self.assertRaises(LedgerMissing):
            worker.get("anything")
        self.assertFalse(state.exists())
        with self.assertRaises(LedgerMissing):
            Supervisor(state / "runs.sqlite", create=False)
        self.assertFalse(state.exists())

    def run_worker_main(self, db, **patches):
        """Run the fixture-worker entry in-process, as a spawned worker runs it."""
        argv = ["run_supervisor", "_fixture-worker", str(db),
                json.dumps([sys.executable, str(self.child), str(self.events), "0", "0"]),
                json.dumps({"reviewer": 1, "fixer": 1, "adjudicator": 1}), "60", "120"]
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(Supervisor, name, value))
            stack.enter_context(patch.object(sys, "argv", argv))
            # A spawned worker always has HOME and HERMES_HOME (_spawn sets both); the fixture
            # child's environment is built from them.
            stack.enter_context(patch.dict(os.environ, {"REVIEW_LOOP_TEST_FIXTURE": "1",
                                                        "HOME": str(self.root),
                                                        "HERMES_HOME": str(self.root)}))
            stack.enter_context(contextlib.redirect_stderr(err))
            run_supervisor.main()  # returning normally is the worker's exit status 0
        return own_lines(err)

    def pending_row(self, state):
        host = Supervisor(state / "runs.sqlite", fixture_mode=True, fixture_command=[
            sys.executable, str(self.child), str(self.events), "0", "0"])
        with patch.object(Supervisor, "_spawn"):
            host.enqueue("m", "o/r", 1, "a", "reviewer")
        return host

    def test_worker_exits_quietly_when_ledger_vanishes_before_claim(self):
        state = self.root / "claim-state"
        self.pending_row(state)
        claim = Supervisor._claim
        def vanish(sup):
            shutil.rmtree(state)
            return claim(sup)
        lines = self.run_worker_main(state / "runs.sqlite", _claim=vanish)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("is gone", lines[0])
        self.assertFalse(state.exists())
        self.assertFalse(self.events.exists())

    def test_worker_exits_quietly_when_ledger_vanishes_mid_run(self):
        # The child has run; the ledger goes before the worker records the outcome, so both
        # complete_uncertain() and recover() in _run_fixture's finally meet a missing ledger.
        state = self.root / "run-state"
        self.pending_row(state)
        complete = Supervisor.complete_uncertain
        def vanish(sup, *args, **kwargs):
            shutil.rmtree(state, ignore_errors=True)
            return complete(sup, *args, **kwargs)
        lines = self.run_worker_main(state / "runs.sqlite", complete_uncertain=vanish)
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse(state.exists())
        self.assertEqual(len(self.launches()), 1)

    def test_heartbeat_tolerates_a_vanished_ledger(self):
        state = self.root / "beat-state"
        Supervisor(state / "runs.sqlite")
        worker = Supervisor(state / "runs.sqlite", create=False)
        shutil.rmtree(state)
        class OneBeat:
            calls = 0
            def wait(self, _):
                self.calls += 1
                return self.calls > 1
        worker._heartbeat("run", "owner", OneBeat())  # must not raise
        self.assertFalse(state.exists())

    def foreign_ledgers(self):
        """Files at a ledger path that are not a review-loop ledger, by shape."""
        shapes = {"empty": b"", "not-sqlite": b"not a database at all\n" * 4,
                  "corrupt": b"SQLite format 3\x00" + b"\xff" * 4080}
        for name, build in (("foreign-sqlite", "CREATE TABLE someone_elses(x); "
                                                "INSERT INTO someone_elses VALUES (1);"),
                            # Only the plugin's runs table, with a claimable pending row.
                            ("lookalike", run_supervisor.SCHEMA.split(";")[0] + "; INSERT INTO runs"
                             "(id,delivery,repo,pr,head,seat,state,created,updated) VALUES "
                             "('dead','d','o/r',1,'h','reviewer','pending',0,0);")):
            path = self.root / f"build-{name}.sqlite"
            with contextlib.closing(sqlite3.connect(path)) as con:
                con.executescript(build)
            shapes[name] = path.read_bytes()
        return shapes

    def test_worker_refuses_an_empty_or_foreign_ledger_file(self):
        for name, content in self.foreign_ledgers().items():
            with self.subTest(name):
                db = self.root / f"{name}.sqlite"
                db.write_bytes(content)
                with self.assertRaises(LedgerMissing):
                    Supervisor(db, create=False)
                # Never adopted, re-schema'd or claimed from.
                self.assertEqual(db.read_bytes(), content)

    def test_worker_exits_quietly_when_ledger_is_replaced_mid_run(self):
        # The child has run; the ledger is swapped for something else before the worker
        # records the outcome. The worker writes nothing into the replacement.
        for name, content in self.foreign_ledgers().items():
            with self.subTest(name):
                state = self.root / f"swap-{name}"
                self.pending_row(state)
                db = state / "runs.sqlite"
                complete = Supervisor.complete_uncertain
                def swap(sup, *args, **kwargs):
                    for path in state.iterdir():
                        path.unlink()
                    db.write_bytes(content)
                    return complete(sup, *args, **kwargs)
                lines = self.run_worker_main(db, complete_uncertain=swap)
                self.assertEqual(len(lines), 1, lines)
                self.assertEqual(db.read_bytes(), content)
                self.assertEqual(sorted(p.name for p in state.iterdir()), ["runs.sqlite"])

    def test_worker_turn_never_recreates_a_deleted_state_dir(self):
        state = self.root / "turn-state"
        loop = {"repo": "o/r", "state_dir": str(state)}
        scope = broker_ipc.RunScope("o/r", 1, "a" * 40, "reviewer", "b")
        with patch.dict(os.environ, {"REVIEW_LOOP_WORKER": "1"}), \
                self.assertRaises(HostStateGone):
            trusted_turn.run_turn(loop, scope, source=self.root / "none", venv=self.root,
                                  runtime=self.root, rust=self.root, upstream="https://x",
                                  key="k", model="m", prompt="p",
                                  work_root=state / "isolated-runs")
        self.assertFalse(state.exists())

    def test_worker_seat_lock_never_creates_the_lock_dir(self):
        home = self.root / "hermes-home"
        locks = home / "state" / "review-loop-seat-locks"
        with patch.object(config, "home", return_value=home):
            with patch.dict(os.environ, {"REVIEW_LOOP_WORKER": "1"}):
                with seat_model.profile_lock("default"):
                    pass  # the in-process lock still serializes this worker's threads
            self.assertFalse(locks.exists())
            with seat_model.profile_lock("default"):
                pass
            self.assertTrue(locks.is_dir())  # the host still creates it

    def test_gate_creates_the_worker_dirs_before_enqueue(self):
        home = self.root / "hermes-home"
        state = self.root / "loop-state"
        loop = {"repo": "o/r", "state_dir": str(state)}
        with patch.object(config, "home", return_value=home), \
                patch.object(config, "seat_concurrency", return_value=1), \
                patch.object(run_supervisor, "Supervisor"):
            gate.enqueue_isolated(loop, "reviewer", 1, "a" * 40)
        self.assertTrue(state.is_dir())
        self.assertEqual((state / "isolated-runs").stat().st_mode & 0o777, 0o700)
        self.assertTrue((home / "state" / "review-loop-seat-locks").is_dir())

    def test_worker_stderr_goes_to_a_bounded_log_beside_the_ledger(self):
        sup = self.supervisor()
        log = self.root / "ledger.sqlite.workers.log"
        log.write_bytes(b"x" * (run_supervisor.WORKER_LOG_MAX + 1))
        seen = []
        def popen(*args, stderr=None, **kwargs):
            # The inode the worker's stderr descriptor points at.
            seen.append(stderr if stderr == subprocess.DEVNULL else os.fstat(stderr.fileno()).st_ino)
            return unittest.mock.MagicMock()
        with patch("review_loop.run_supervisor.subprocess.Popen", side_effect=popen):
            sup._spawn()
        self.assertEqual(seen, [log.stat().st_ino])
        self.assertEqual(log.stat().st_size, 0)  # rotated, not grown without bound
        self.assertEqual((self.root / "ledger.sqlite.workers.log.1").stat().st_size,
                         run_supervisor.WORKER_LOG_MAX + 1)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_worker_that_finds_no_ledger_leaves_its_line_in_the_log(self):
        sup = self.supervisor()
        (self.root / "ledger.sqlite").write_bytes(b"")  # replaced: a worker must refuse it
        sup._spawn()
        self.assertEqual(_WORKERS[-1].wait(timeout=WORKER_EXIT_TIMEOUT), 0)
        lines = own_lines(io.StringIO((self.root / "ledger.sqlite.workers.log").read_text()))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("nothing to run", lines[0])
        self.assertEqual((self.root / "ledger.sqlite").read_bytes(), b"")

    def test_host_reports_a_vanished_ledger_loudly_and_once(self):
        db = self.root / "watched" / "runs.sqlite"
        for quiet in ("first creation", "reopen"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                Supervisor(db)
            self.assertEqual(own_lines(err), [], quiet)
        for path in db.parent.glob("runs.sqlite*"):
            if not path.name.endswith(".present"):
                path.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sup = Supervisor(db)
        self.assertEqual(len(own_lines(err)), 1, err.getvalue())
        self.assertIn("vanished", own_lines(err)[0])
        sent = []
        sup.notify(sent.append)
        sup.notify(sent.append)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("vanished", sent[0])

    def test_a_wiped_state_dir_is_reported_once_and_a_fresh_install_is_silent(self):
        # The beside-ledger marker goes with the state dir; the host-owned one in the loop
        # config dir survives it (#108).
        from scripts import watchdog
        presence = self.root / "review-loops.d" / ".ledger-present"
        state = self.root / "state"
        db = state / "runs.sqlite"
        err, sent = io.StringIO(), []
        with contextlib.redirect_stderr(err):
            Supervisor(db, presence=presence).notify(sent.append)  # fresh install
        self.assertEqual((own_lines(err), sent), ([], []))
        self.assertTrue(presence.is_file())
        shutil.rmtree(state)
        with contextlib.redirect_stderr(err):
            sup = Supervisor(db, presence=presence)
            sup.notify(sent.append)
            sup.notify(sent.append)
            Supervisor(db, presence=presence).notify(sent.append)  # reported once, not again
        self.assertEqual(len(own_lines(err)), 1, err.getvalue())
        self.assertIn("vanished", own_lines(err)[0])
        self.assertEqual(len(sent), 1, sent)
        # The watchdog finds it too, with no gate run in between.
        shutil.rmtree(state)
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            watchdog.sweep_ledger(db, presence=presence)
            watchdog.sweep_ledger(db, presence=presence)
        self.assertEqual(out.getvalue().count("vanished"), 1, out.getvalue())

    def test_forgetting_the_ledger_makes_the_next_install_fresh(self):
        presence = self.root / "review-loops.d" / ".ledger-present"
        state = self.root / "state"
        Supervisor(state / "runs.sqlite", presence=presence)
        shutil.rmtree(state)
        with patch.object(config, "config_dir", return_value=presence.parent):
            self.assertEqual(run_supervisor.presence_marker(), presence)
            self.assertTrue(run_supervisor.forget_ledger_presence())
            self.assertFalse(run_supervisor.forget_ledger_presence())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            Supervisor(state / "runs.sqlite", presence=presence)
        self.assertEqual(own_lines(err), [])

    def test_uninstalling_the_last_loop_forgets_the_ledger(self):
        from argparse import Namespace
        from review_loop import cli
        cfg = self.root / "review-loops.d"
        cfg.mkdir()
        for name in ("a", "b"):
            (cfg / f"{name}.json").write_text("{}")
        (cfg / ".ledger-present").write_text("x")
        with patch.object(config, "config_dir", return_value=cfg), \
                patch.object(config, "load_id", side_effect=lambda name: {"id": name}), \
                patch.object(cli, "_routes_of", return_value={}), \
                patch.object(cli.route_intent, "forget"), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_uninstall(Namespace(loop="a", keep_config=False))
            self.assertTrue((cfg / ".ledger-present").exists())  # loop b still configured
            cli.cmd_uninstall(Namespace(loop="b", keep_config=False))
        self.assertFalse((cfg / ".ledger-present").exists())

    def test_watchdog_notices_a_vanished_ledger_once(self):
        from scripts import watchdog
        db = self.root / "wd" / "runs.sqlite"
        Supervisor(db)
        db.unlink()
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            watchdog.sweep_ledger(db)
            watchdog.sweep_ledger(db)
        self.assertEqual(out.getvalue().count("vanished"), 1, out.getvalue())
        missing = self.root / "never" / "runs.sqlite"
        self.assertEqual(watchdog.sweep_ledger(missing), [])
        self.assertFalse(missing.parent.exists())  # no ledger ever: nothing created

    def test_host_adopts_an_empty_ledger_file_with_a_diagnostic(self):
        db = self.root / "empty-host.sqlite"
        db.write_bytes(b"")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sup = Supervisor(db)
        self.assertIn("empty", err.getvalue())
        self.assertIn(str(db), err.getvalue())
        self.assertEqual(sup.status(), [])

    def test_worker_audit_never_recreates_a_deleted_state_dir(self):
        state = self.root / "loop-state"
        loop = {"state_dir": str(state)}
        err = io.StringIO()
        with patch.dict(os.environ, {"REVIEW_LOOP_WORKER": "1"}), contextlib.redirect_stderr(err):
            broker._audit(loop, "o/r", 1, "a" * 40, "b", "reviewer", "review", "login")
        self.assertFalse(state.exists())
        self.assertEqual(len(own_lines(err)), 1, err.getvalue())
        # The host still creates it, as before.
        broker._audit(loop, "o/r", 1, "a" * 40, "b", "reviewer", "review", "login")
        self.assertTrue((state / "broker-audit.jsonl").is_file())

    def test_spawned_worker_is_marked_as_a_worker(self):
        # hostdirs.ensure refuses to create host state only in a process marked this way.
        sup = self.supervisor()
        with patch("review_loop.run_supervisor.subprocess.Popen") as popen:
            sup._spawn()
        self.assertEqual(popen.call_args.kwargs["env"].get("REVIEW_LOOP_WORKER"), "1")

    def test_host_enqueue_creates_a_missing_ledger_dir(self):
        state = self.root / "fresh-state"
        sup = Supervisor(state / "runs.sqlite", fixture_mode=True, fixture_command=[
            sys.executable, str(self.child), str(self.events), "0", "0"])
        self.assertTrue((state / "runs.sqlite").is_file())
        sup.enqueue("fresh", "o/r", 1, "a", "reviewer")
        self.wait(sup, "fresh", "succeeded")
        self.assertEqual(len(self.launches()), 1)

    def test_child_failure_and_timeout_release_seat(self):
        # Three phases share one ledger, each with its own fixture child and timeout. A worker
        # claims whatever row is pending with the config it was spawned with, so a phase's
        # idle straggler (spawned by its worker's recover()) could claim the next phase's row
        # under load (#108). Each phase therefore waits until its workers are gone.
        sup = self.supervisor(rc=7)
        sup.enqueue("bad", "o/r", 1, "a", "reviewer")
        self.assertEqual(self.wait(sup, "bad", "failed")["outcome"], 7)
        wait_for_workers(self.root)
        # The child sleeps far past its timeout, so only the timeout can end it, however
        # slow the machine is; the timeout is still short enough to keep the test quick.
        slow = self.supervisor(delay=60, child_timeout=1)
        slow.enqueue("slow", "o/r", 2, "b", "reviewer")
        row = self.wait(slow, "slow", "failed", timeout=20)
        self.assertEqual((row["error"], row["outcome"]), ("child timeout", None))
        wait_for_workers(self.root)
        ok = self.supervisor()
        ok.enqueue("ok", "o/r", 3, "c", "reviewer")
        row = self.wait(ok, "ok", "succeeded")
        self.assertEqual((row["outcome"], row["error"]), (0, None))
        self.assertEqual(len(self.launches()), 3)

    def test_spawn_failure_remains_recoverable_before_claim(self):
        sup = self.supervisor()
        original = sup._spawn
        sup._spawn = lambda: (_ for _ in ()).throw(OSError("injected"))
        with self.assertRaises(OSError):
            sup.enqueue("d", "o/r", 1, "a", "reviewer")
        self.assertEqual(sup.get("d")["state"], "pending")
        sup._spawn = original
        sup.recover()
        self.wait(sup, "d", "succeeded")

    def test_different_host_home_and_hermes_home_launches(self):
        host = self.root / "host-home"
        hermes = self.root / "hermes-home"
        host.mkdir()
        hermes.mkdir()
        sup = self.supervisor()
        with patch.dict(os.environ, {"HOME": str(host), "HERMES_HOME": str(hermes)}):
            sup.enqueue("split-home", "o/r", 8, "head", "reviewer")
            self.wait(sup, "split-home", "succeeded")
        self.assertEqual(len(self.launches()), 1)

    def test_running_lease_heartbeats_and_completion_after_expiry(self):
        # The child runs until released, so the run is live for as long as the test needs.
        # Heartbeats come every lease/3 = 1s; a 3s lease lets a beat be ~2s late (a loaded
        # runner, a SQLite stall) before the run is really lost.
        sup = self.gated_supervisor(lease_seconds=3, child_timeout=60)
        sup.enqueue("heartbeat", "o/r", 9, "head", "reviewer")
        launched = self.wait(sup, "heartbeat", "running")
        # Wait until the lease granted at launch has passed, so only a heartbeat can have
        # kept the run alive, and the renewed lease has at least a second left to cover the
        # recover() below. Observed ledger state, not a fixed sleep.
        until = time.monotonic() + 20
        while True:
            row, now = sup.get("heartbeat"), time.time()
            self.assertEqual(row["state"], "running")
            if now > launched["lease"] and row["lease"] - now >= 1:
                break
            if time.monotonic() > until:
                log = self.root / "ledger.sqlite.workers.log"
                self.fail(f"no heartbeat renewed the lease past its launch grant: {row}; "
                          f"worker log: {log.read_text() if log.exists() else '(none)'}")
            time.sleep(0.05)
        sup.recover()
        self.assertEqual(sup.get("heartbeat")["state"], "running")
        self.release()
        self.wait(sup, "heartbeat", "succeeded")
        wait_for_workers(self.root)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='running', lease=0 WHERE delivery='heartbeat'")
        sup.recover()
        self.assertEqual(sup.get("heartbeat")["state"], "uncertain")
        row = sup.get("heartbeat")
        sup.complete_uncertain(row["id"], row["owner"], 0)
        self.assertEqual(sup.get("heartbeat")["state"], "succeeded")

    def test_recovery_retries_prelaunch_but_quarantines_post_intent(self):
        sup = self.supervisor()
        sup.enqueue("before", "o/r", 1, "a", "reviewer")
        self.wait(sup, "before", "succeeded")
        # Inject expired state transitions while preserving a real durable row.
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='claimed', attempts=1, lease=0 WHERE delivery='before'")
        sup.recover()
        self.wait(sup, "before", "succeeded")
        self.assertEqual(len(self.launches()), 2)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='launching', lease=0 WHERE delivery='before'")
        sup.recover()
        self.assertEqual(sup.get("before")["state"], "uncertain")
        sup.enqueue("held", "o/r", 1, "new-head", "fixer")
        # The worker enqueue started has exited without claiming: "held" stays pending.
        wait_for_workers(self.root)
        self.assertEqual(sup.get("held")["state"], "pending")
        self.assertEqual(len(self.launches()), 2)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='claimed', attempts=?, lease=0 WHERE delivery='held'",
                       (MAX_ATTEMPTS,))
        sup.recover()
        self.assertEqual(sup.get("held")["state"], "failed")

    def test_uncertain_seat_requires_explicit_dead_worker_reconciliation(self):
        sup = self.supervisor()
        sup.enqueue("old", "o/r", 11, "head", "reviewer")
        self.wait(sup, "old", "succeeded")
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET state='running', pid=?, lease=0 "
                       "WHERE delivery='old'", (os.getpid(),))
        sup.recover()
        old = sup.get("old")
        sup.enqueue("next", "o/r", 12, "head", "reviewer")
        self.assertEqual(sup.get("next")["state"], "pending")
        with self.assertRaises(ValueError):
            sup.reconcile_uncertain(old["id"], reason='inspected')
        with self.assertRaisesRegex(ValueError, 'PID exists'):
            sup.reconcile_uncertain(old["id"], reason='inspected',
                                    acknowledge_no_live_worker=True)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE runs SET pid=NULL WHERE id=?", (old["id"],))
        self.assertTrue(sup.reconcile_uncertain(old["id"], reason='confirmed external state',
                        acknowledge_no_live_worker=True))
        self.assertEqual(sup.get("old")["state"], "failed")
        sup.recover()
        self.wait(sup, "next", "succeeded")
        self.assertEqual(len(self.launches()), 2)


class ReviewerClaimConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The tests below set production_config after the first enqueue, so a later enqueue
        # spawns a (failing) detached production worker that names this temp dir.
        self.addCleanup(wait_for_workers, Path(self.tmp.name))
        # A fixer claim takes the host policy lock under $HERMES_HOME/review-loops.d: keep it in
        # this fixture, never the operator's home (#109).
        env = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name,
                                      'REVIEW_LOOP_CONFIG_DIR': str(Path(self.tmp.name) / 'review-loops.d')})
        env.start()
        self.addCleanup(env.stop)
        self.db = Path(self.tmp.name) / 'ledger.sqlite'
        self.sup = Supervisor(self.db)
        self.sup.enqueue('review', 'o/r', 1, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending' WHERE delivery='review'")
        # Exercise the production generation gate without launching a worker.
        self.sup.production_config = Path(self.tmp.name) / 'unused-config'
        # A configured loop always has a state_dir: the claim reads its retarget holds there.
        self.loop = {'repo': 'o/r', 'base': 'main', 'read_token': 'read',
                     'state_dir': str(Path(self.tmp.name) / 'state')}

    def pull(self, base_sha='b' * 40):
        return {'number': 1, 'state': 'open', 'draft': False,
                'base': {'ref': 'main', 'sha': base_sha,
                         'repo': {'full_name': 'o/r'}},
                'head': {'sha': 'a' * 40, 'repo': {'full_name': 'o/r'}}}

    def test_slow_read_does_not_hold_writer_lock_and_competing_claim_is_atomic(self):
        entered, both_entered, release = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        calls = 0
        errors = []
        def slow_api(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                entered.set()
                if calls == 2:
                    both_entered.set()
            if not release.wait(5):
                raise TimeoutError('test timed out waiting for release')
            return self.pull()
        def claim():
            try:
                return self.sup._claim()
            except Exception as exc:
                errors.append(exc)
                return None
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=slow_api):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(claim)
                try:
                    self.assertTrue(entered.wait(2))
                    start = time.monotonic()
                    with patch.object(self.sup, '_spawn'):
                        self.sup.enqueue('unrelated', 'o/r', 2, 'c' * 40, 'fixer')
                    self.assertLess(time.monotonic() - start, 2)
                    with sqlite3.connect(self.db, timeout=1) as con:
                        con.execute("UPDATE runs SET state='blocked' WHERE delivery='unrelated'")
                    second = pool.submit(claim)
                    self.assertTrue(both_entered.wait(2))
                finally:
                    release.set()
                claims = [first.result(timeout=5), second.result(timeout=5)]
        self.assertFalse(errors)
        self.assertEqual(sum(c is not None for c in claims), 1)
        self.assertEqual(self.sup.get('review')['state'], 'claimed')
        self.assertEqual(self.sup.get('unrelated')['state'], 'blocked')

    def test_stale_row_changed_during_read_is_not_claimed(self):
        entered, release = threading.Event(), threading.Event()
        def slow_api(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test timed out waiting for release')
            return self.pull()
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=slow_api):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.sup._claim)
                try:
                    self.assertTrue(entered.wait(2))
                    with sqlite3.connect(self.db, timeout=1) as con:
                        con.execute("UPDATE runs SET generation='changed' WHERE delivery='review'")
                finally:
                    release.set()
                self.assertIsNone(future.result(timeout=5))
        self.assertEqual(self.sup.get('review')['state'], 'pending')
        self.assertEqual(self.sup.get('review')['generation'], 'changed')

    def test_unavailable_generation_fails_without_claim(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', return_value=self.pull(base_sha='invalid')):
            self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('review')['state'], 'failed')
        self.assertEqual(self.sup.get('review')['generation'], None)

    def test_transient_generation_read_remains_pending_then_claims(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=[TimeoutError('temporary'), self.pull()]):
            self.assertIsNone(self.sup._claim())
            self.assertEqual(self.sup.get('review')['state'], 'pending')
            self.assertEqual(self.sup.get('review')['attempts'], 0)
            self.assertIsNotNone(self.sup._claim())
        self.assertEqual(self.sup.get('review')['state'], 'claimed')

    def test_same_delivery_rearms_pending_after_transient_read(self):
        with patch('review_loop.config.by_repo', return_value=self.loop), \
             patch('review_loop.gh.api', side_effect=TimeoutError('temporary')):
            self.assertIsNone(self.sup._claim())
        with patch.object(self.sup, '_spawn') as spawn:
            self.sup.enqueue('review', 'o/r', 1, 'a' * 40, 'reviewer')
            spawn.assert_called_once_with()

    def test_dismissed_same_head_uses_new_turn_but_redelivery_deduplicates(self):
        head = 'a' * 40
        self.sup.enqueue('first-delivery', 'o/r', 2, head, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded' WHERE delivery='first-delivery'")
        self.sup.enqueue('dismissed-42', 'o/r', 2, head, 'reviewer', turn_key='dismissed:42')
        self.sup.enqueue('dismissed-42-replay', 'o/r', 2, head, 'reviewer', turn_key='dismissed:42')
        with sqlite3.connect(self.db) as con:
            rows = con.execute('SELECT delivery,turn_key FROM runs WHERE pr=2 ORDER BY created,id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({key for _, key in rows}, {'', 'dismissed:42'})

    def test_new_turn_never_bypasses_active_same_pr_run(self):
        self.sup.enqueue('first-delivery', 'o/r', 2, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running' WHERE delivery='first-delivery'")
        self.sup.enqueue('dismissed-42', 'o/r', 2, 'a' * 40, 'reviewer', turn_key='dismissed:42')
        self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('dismissed-42')['state'], 'pending')

    def test_legacy_unique_index_migrates_without_losing_existing_run(self):
        self.sup.enqueue('initial', 'o/r', 4, 'a' * 40, 'reviewer')
        with sqlite3.connect(self.db) as con:
            con.execute('DROP INDEX runs_turn')
            con.execute('CREATE UNIQUE INDEX runs_turn ON runs(repo,pr,head,seat)')
            con.execute('ALTER TABLE runs DROP COLUMN turn_key')
        migrated = Supervisor(self.db)
        migrated.enqueue('dismissed', 'o/r', 4, 'a' * 40, 'reviewer', turn_key='dismissed:42')
        with sqlite3.connect(self.db) as con:
            rows = con.execute('SELECT delivery,turn_key FROM runs WHERE pr=4 ORDER BY delivery').fetchall()
        self.assertEqual(rows, [('dismissed', 'dismissed:42'), ('initial', '')])

    def test_queued_fixer_checks_latest_live_verdict_before_claim(self):
        self.sup.enqueue('fix', 'o/r', 3, 'a' * 40, 'fixer')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending',push_admitted=1 WHERE delivery='fix'")
            con.execute("UPDATE runs SET state='blocked' WHERE delivery='review'")
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': 'a' * 40,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}},
                   {'id': 42, 'state': 'APPROVED', 'commit_id': 'a' * 40,
                    'submitted_at': '2026-01-01T00:01:00Z', 'user': {'login': 'review'}}]
        loop = {**self.loop, 'reviewers': ['review'], 'unattended_fixer_push': True}
        with patch('review_loop.config.by_repo', return_value=loop), \
             patch('review_loop.gh.api', return_value=self.pull()), \
             patch('review_loop.gh.reviews', return_value=reviews):
            self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get('fix')['state'], 'cancelled')
        self.assertEqual(self.sup.get('fix')['error'], 'fixer verdict superseded')


if __name__ == "__main__":
    unittest.main()
