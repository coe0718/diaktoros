"""#1046: the reviewer gate as an entry point that takes its facts as arguments, and #1334: a
maintainer's reconsider of a head that already has a verdict.

The real reviewer gate decides, against the harness's fake GitHub; the concurrency test drives
two gate calls on two threads at once.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import os
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402

from diaktoros import config, explain, gate_failures, gh, one_shot_action as actions, util  # noqa: E402
from diaktoros.run_supervisor import RECONSIDER_KEY  # noqa: E402


def approved(head=None):
    """PR 7, by the fixer, whose head the reviewer approved."""
    head = head or t.HEAD_A
    pr = t.pr(7, head=head)
    pr["reviews"] = [t.review(t.REVIEWER, "APPROVED", head=head, rid=11)]
    return pr


class Harness(unittest.TestCase):
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

    def loop(self):
        return config.load_id("widgets")

    def queued(self, number=7):
        return t.load_state("pending.json").get("reviewer", {}).get(f"{t.REPO}#{number}", {})


class EntryPoint(Harness):
    def test_the_entry_point_is_driven_directly_and_returns_its_outcome(self):
        t.reset(prs={"7": t.pr(7)})
        stdin, stdout = sys.stdin, sys.stdout
        result = actions.review(self.loop(), 7)
        self.assertIs(sys.stdin, stdin)
        self.assertIs(sys.stdout, stdout)
        self.assertEqual((result["code"], result["head"]), (0, t.HEAD_A))
        self.assertTrue(result["lines"][-1].startswith(
            "#7 @ aaaaaaa: review queued, waiting — isolated worker unavailable"), result["lines"])
        self.assertTrue(t.held("reviewer", 7, t.HEAD_A))

    def test_a_confirmed_head_that_moved_is_refused_before_the_gate(self):
        t.reset(prs={"7": t.pr(7, head=t.HEAD_B)})
        with mock.patch.object(actions, "_run_reviewer") as gate_call:
            result = actions.review(self.loop(), 7, expected_head=t.HEAD_A)
        self.assertEqual(result["code"], 2)
        self.assertIn("refused: the PR is at bbbbbbb now, not the aaaaaaa you confirmed",
                      result["lines"][0])
        gate_call.assert_not_called()
        self.assertFalse(t.held("reviewer", 7, t.HEAD_B))

    def test_a_crashing_gate_is_reported_and_exits_2(self):
        t.reset(prs={"7": t.pr(7)})
        entry = mock.Mock()
        entry.decide.side_effect = RuntimeError("/private/path gate blew up")
        with mock.patch.object(actions, "_script", return_value=entry):
            result = actions.review(self.loop(), 7)
            rc, out = t.run_cli(t.parser_for({}).parse_args(
                ["review", "--loop", "widgets", "--pr", "7"]))
        self.assertEqual(result["code"], 2)
        self.assertIn("the gate did not finish — it crashed (RuntimeError)", result["lines"][-1])
        self.assertNotIn("/private/path", result["lines"][-1])
        self.assertEqual(rc, 2, out)
        self.assertIn("the gate did not finish", out)

    def test_a_migration_holds_the_review(self):
        t.reset(prs={"7": t.pr(7)})
        with mock.patch("diaktoros.migrate.migrating", return_value={"from": "x"}), \
                mock.patch.object(actions, "_run_reviewer") as gate_call:
            result = actions.review(self.loop(), 7)
        self.assertEqual(result["code"], 2)
        self.assertIn("a migration holds the install", result["lines"][0])
        gate_call.assert_not_called()

    def test_failed_reads_are_recorded_only_inside_a_call_that_asked(self):
        loop = {"state_dir": "/tmp/x", "read_token": "r"}
        with mock.patch("diaktoros.state.state_for") as state_for, \
                mock.patch.object(gh, "_RECORD_FAILURES", False):
            with gh.gate_scope(10**12):
                gh.record_failure(loop, "GET", "/a", "HTTP 502")
                state_for.assert_not_called()
                gh.record_failures()
                gh.record_failure(loop, "GET", "/a", "HTTP 502")
                self.assertEqual(state_for.call_count, 1)
            gh.record_failure(loop, "GET", "/a", "HTTP 502")
            self.assertEqual(state_for.call_count, 1, "outside the call, the process's flag holds")

    def test_the_gate_answers_a_call_without_printing_the_route_answer(self):
        out = io.StringIO()
        with util.gate_call() as call, contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit):
            util.silence("draft PR")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(util.logged(call["logs"]), ["draft PR"])

    def test_two_gate_calls_at_once_never_see_each_others_state(self):
        # Each call logs, records its decision, sets a budget and the failed-read flag, then waits
        # for the other to do the same before answering: any shared state would bleed.
        barrier = threading.Barrier(2, timeout=10)
        recorded = {7: [], 8: []}
        seen = {}

        def decide(payload, reconsider=None):
            number = payload["number"]
            util.set_decision_recorder(lambda reason, decision: recorded[number].append(reason))
            gh.begin_gate(10**12 + number)
            gh.record_failures()
            util.log(f"looking at #{number}")
            barrier.wait()
            seen[number] = (gh.remaining() > 10**11, int(gh._gate()["deadline"]) - 10**12)
            barrier.wait()
            util.silence(f"declined #{number}")

        entry = mock.Mock(decide=decide)
        results = {}
        with mock.patch.object(actions, "_script", return_value=entry), \
                mock.patch.object(gh, "_RECORD_FAILURES", False), \
                mock.patch.dict(gh._GATE, {"deadline": None, "errors": None, "per_call": 30.0}):
            threads = [threading.Thread(target=lambda n=n: results.update(
                {n: actions._run_reviewer({"number": n})})) for n in (7, 8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertIsNone(gh._GATE["deadline"], "the process budget was never set")
            self.assertFalse(gh._RECORD_FAILURES, "nor the process's failed-read flag")
        self.assertEqual(util.logged(results[7]), ["looking at #7", "declined #7"])
        self.assertEqual(util.logged(results[8]), ["looking at #8", "declined #8"])
        self.assertEqual(recorded, {7: ["declined #7"], 8: ["declined #8"]})
        self.assertEqual(seen, {7: (True, 7), 8: (True, 8)})


class Reconsider(Harness):
    def setUp(self):
        # The turn key the gate hands the ledger (the harness has no worker runtime, so the
        # real enqueue would hold the turn in the seat queue, which keeps no key).
        self.keys = []
        enqueue = mock.patch("diaktoros.gate.enqueue_isolated",
                             side_effect=lambda loop, seat, n, head, turn_key='': (
                                 self.keys.append((seat, n, head, turn_key)) or "enqueued"))
        enqueue.start()
        self.addCleanup(enqueue.stop)

    def reconsider(self, reason="the requirement in the issue must be met, not deferred"):
        return actions.review(self.loop(), 7, reconsider=reason)

    def test_a_maintainer_sends_an_approved_head_back_for_one_fresh_review(self):
        t.reset(prs={"7": approved()})
        plain = actions.review(self.loop(), 7)
        self.assertIn("already has a reviewer's verdict", plain["lines"][-1])
        self.assertEqual(self.keys, [])
        self.reconsider()
        self.assertEqual(self.keys, [("reviewer", 7, t.HEAD_A, RECONSIDER_KEY)])
        from diaktoros import state as state_mod
        entry = state_mod.state_for(self.loop()).reconsider_get(7, t.HEAD_A)
        self.assertEqual(entry["reason"], "the requirement in the issue must be met, not deferred")

    def test_a_queued_reconsider_is_recorded_as_an_operator_step(self):
        from diaktoros import ledger, run_supervisor
        t.reset(prs={"7": approved()})
        counts = iter([0, 0, 1])   # the reconsider check, the count before the gate, after it
        with mock.patch.object(actions, "_reviewer_runs", side_effect=lambda *a: next(counts)):
            result = self.reconsider()
        self.assertEqual(result["code"], 0, result["lines"])
        with ledger.connect(run_supervisor.production_ledger()) as con:
            actions_seen = con.execute("SELECT action FROM operator_actions WHERE pr=7").fetchall()
        self.assertEqual(actions_seen, [("reconsider",)])

    def test_a_second_reconsider_at_the_same_head_is_refused(self):
        t.reset(prs={"7": approved()})
        with mock.patch.object(actions, "_reviewer_runs",
                               side_effect=lambda repo, n, head, key=None: 1 if key else 0):
            result = self.reconsider()
        self.assertEqual(result["code"], 1)
        self.assertIn("already reconsidered (one per head)", result["lines"][0])
        self.assertEqual(self.keys, [])

    def test_a_reconsider_needs_a_reason_and_a_reviewed_head(self):
        t.reset(prs={"7": approved()})
        self.assertIn("needs a reason", self.reconsider("  ")["lines"][0])
        t.reset(prs={"7": t.pr(7)})
        result = self.reconsider()
        self.assertEqual(result["code"], 1)
        self.assertIn("has no reviewer's verdict yet", result["lines"][0])
        self.assertEqual(self.keys, [])

    def test_no_webhook_can_reconsider_a_reviewed_head(self):
        # The reconsider is the host's argument, never a payload field: a maintainer's (or
        # anyone's) re-request at a reviewed head is still declined by the webhook gate.
        t.reset(prs={"7": approved()})
        payload = {"action": "review_requested", "number": 7, "pull_request": approved(),
                   "repository": {"full_name": t.REPO}, "requested_reviewer": {"login": t.SEAT},
                   "sender": {"login": "a-maintainer"}, "reconsider": "please"}
        kind, _out, err = t.run("gate_reviewer.py", payload)
        self.assertEqual(kind, "SILENT")
        self.assertFalse(t.held("reviewer", 7, t.HEAD_A))

    def test_the_cli_flag_reaches_the_entry_point(self):
        from diaktoros import cli
        t.reset(prs={"7": approved()})
        t.run_cli(t.parser_for({}).parse_args(
            ["review", "--loop", "widgets", "--pr", "7", "--reconsider", "meet the spec"]))
        self.assertEqual(self.keys, [("reviewer", 7, t.HEAD_A, RECONSIDER_KEY)])
        rc, out = t.run_cli(t.parser_for({}).parse_args(
            ["review", "--loop", "widgets", "--pr", "7", "--reconsider", "x", "--another-round"]))
        self.assertEqual(rc, 2)
        self.assertIn("use one", out)
        del cli


class ReconsiderTurn(unittest.TestCase):
    """The queued turn: its claim is not superseded by the head's verdict, and its prompt
    carries the maintainer's reason."""

    def test_the_claim_keeps_a_reconsider_at_a_reviewed_head(self):
        from diaktoros.run_supervisor import turns
        source = pathlib.Path(turns.__file__).read_text()
        start = source.index("def _claim_read_reviewer")
        body = source[start:source.index("def _claim_read_live")]
        self.assertIn("run_supervisor.RECONSIDER_KEY", body)
        exempt = body.index("startswith(('retarget:', run_supervisor.DISPUTE_KEY,")
        self.assertLess(exempt, body.index("superseded: reviewed at this head"))

    def test_the_prompt_carries_the_reason_for_a_reconsider_only(self):
        from diaktoros.run_supervisor import turn_prompts
        loop = {"id": "l", "repo": "o/r", "state_dir": "/nonexistent"}
        entry = {"head": "a" * 40, "reason": "meet   the\nspec", "at": 1}
        st = mock.Mock(reconsider_get=mock.Mock(return_value=entry))
        with mock.patch("diaktoros.state.state_for", return_value=st):
            text = turn_prompts.reconsider_section(
                loop, {"turn_key": RECONSIDER_KEY, "pr": 7, "head": "a" * 40})
            other = turn_prompts.reconsider_section(
                loop, {"turn_key": "", "pr": 7, "head": "a" * 40})
        self.assertIn("asked for this head to be reviewed again", text)
        self.assertIn("> meet the spec", text)
        self.assertIn("request changes if it does not meet", text)
        self.assertEqual(other, "")
        st.reconsider_get.assert_called_once_with(7, "a" * 40)


class RecordedFailure(Harness):
    """#1455: a crashed or timed-out in-process gate is in the gate-failure ledger, as the
    webhook path's ``gate_failures.run`` leaves it, so explain, doctor and the sweep see it."""

    def setUp(self):
        t.reset(prs={"7": t.pr(7)})
        self.ledger = gate_failures.Ledger(t.STATE_DIR)
        for path in (self.ledger.path, *self.ledger.payload_dir.glob("*")):
            path.unlink(missing_ok=True)

    def failures(self):
        return [e for e in self.ledger.entries().values() if not e.get("resolved")]

    def test_a_crashed_gate_is_recorded(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=RuntimeError("/private boom")):
            result = actions.review(self.loop(), 7)
        self.assertEqual(result["code"], 2)
        self.assertIn("recorded in the gate-failure ledger", result["lines"][-1])
        (entry,) = self.failures()
        self.assertEqual((entry["gate"], entry["kind"], entry["pr"], entry["head"]),
                         ("gate_reviewer", "crash", 7, t.HEAD_A))
        self.assertEqual(entry["error_type"], "RuntimeError")
        self.assertFalse(entry["redrivable"])

    def test_a_timed_out_gate_is_recorded(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=gh.GateBudgetExceeded("t")):
            result = actions.review(self.loop(), 7)
        self.assertEqual(result["code"], 2)
        (entry,) = self.failures()
        self.assertEqual(entry["kind"], "timeout")

    def test_a_gate_that_exits_nonzero_is_recorded(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=SystemExit(2)):
            actions.review(self.loop(), 7)
        (entry,) = self.failures()
        self.assertEqual((entry["kind"], entry["error"]), ("crash", "exit code 2"))

    def test_the_same_request_failing_again_is_one_entry_and_a_finish_resolves_it(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=RuntimeError("x")):
            actions.review(self.loop(), 7)
            actions.review(self.loop(), 7)
        (entry,) = self.failures()
        self.assertEqual(entry["attempts"], 2)
        actions.review(self.loop(), 7)          # the real gate finishes this time
        self.assertEqual(self.failures(), [])

    def test_explain_and_the_sweep_report_it_without_a_redrive(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=RuntimeError("x")):
            actions.review(self.loop(), 7)
        (entry,) = self.failures()
        line = explain.gate_failure_line(entry)
        self.assertIn("gate_reviewer crash", line)
        self.assertIn("not re-driven", line)
        self.assertNotIn("Redeliver", line)
        said = gate_failures.sweep(self.ledger, "[widgets]", gate_failures.SCRIPTS_DIR,
                                   cooldown_s=3600)
        self.assertEqual(len(said), 1)
        self.assertIn("gate_reviewer crash on #7", said[0])
        self.assertEqual(self.ledger.entries()[entry["id"]]["redrives"], 0)

    def test_an_unwritable_ledger_is_said_not_hidden(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=RuntimeError("x")), \
                mock.patch.object(gate_failures.Ledger, "record", side_effect=OSError("ro")):
            result = actions.review(self.loop(), 7)
        self.assertEqual(result["code"], 2)
        self.assertIn("could NOT be recorded", result["lines"][-1])


class RecordedFailureEpisodes(Harness):
    """#1578 review: the stable in-process key spans separate failure/recovery episodes, a busy
    ledger never hangs the recovery, and a clean SystemExit settles nothing by crashing."""

    def setUp(self):
        t.reset(prs={"7": t.pr(7)})
        self.ledger = gate_failures.Ledger(t.STATE_DIR)
        self.fallback = gate_failures.fallback_ledger()
        for ledger in (self.ledger, self.fallback):
            for path in (ledger.path, *ledger.payload_dir.glob("*")):
                path.unlink(missing_ok=True)

    def failures(self):
        return [e for e in self.ledger.entries().values() if not e.get("resolved")]

    def fail(self):
        with mock.patch.object(actions, "_run_reviewer", side_effect=RuntimeError("x")):
            return actions.review(self.loop(), 7)

    def test_a_failure_in_the_fallback_after_a_resolved_home_episode_is_alerted(self):
        self.fail()
        actions.review(self.loop(), 7)                       # recovers: the home entry resolves
        (key,) = self.ledger.entries()
        self.assertTrue(self.ledger.entries()[key]["resolved"])
        real = gate_failures.Ledger.record
        def home_busy(ledger, *a, **kw):
            if ledger.path == self.ledger.path:
                raise OSError("busy")
            return real(ledger, *a, **kw)
        with mock.patch.object(gate_failures.Ledger, "record", home_busy):
            self.fail()                                      # the next episode lands in the fallback
        self.assertFalse(self.fallback.entries()[key]["resolved"])
        said = gate_failures.sweep(self.fallback, "[widgets]", gate_failures.SCRIPTS_DIR,
                                   cooldown_s=3600)
        self.assertEqual(len(said), 1)
        self.assertIn("gate_reviewer crash on #7", said[0])
        self.assertFalse(self.fallback.entries()[key]["resolved"])

    def test_an_older_overlapping_run_does_not_resolve_a_newer_runs_crash(self):
        """#1593: run A starts, run B starts later and crashes, run A then finishes."""
        def overlapped_crash(payload, **_kw):
            self.fail()                          # run B starts after A did, and crashes
            return []                            # run A finishes
        time.sleep(0.02)
        with mock.patch.object(actions, "_run_reviewer", side_effect=overlapped_crash):
            actions.review(self.loop(), 7)
        (entry,) = self.failures()               # B's crash is still open
        self.assertEqual((entry["gate"], entry["kind"]), ("gate_reviewer", "crash"))

    def test_a_failure_from_before_the_run_started_is_still_resolved(self):
        self.fail()
        self.assertEqual(len(self.failures()), 1)
        time.sleep(0.02)
        actions.review(self.loop(), 7)           # a later run, started after the crash
        self.assertEqual(self.failures(), [])

    def test_the_webhook_path_leaves_a_newer_failure_open_too(self):
        self.fail()
        (key,) = self.ledger.entries()
        last_at = self.ledger.entries()[key]["last_at"]
        self.assertFalse(self.ledger.resolve(key, "x", not_after=last_at - 1))
        self.assertFalse(self.ledger.entries()[key]["resolved"])
        self.assertTrue(self.ledger.resolve(key, "x", not_after=last_at + 1))

    def test_a_fallback_copy_of_a_still_open_home_failure_is_still_a_duplicate(self):
        self.fail()
        (key,) = self.ledger.entries()
        entry = dict(self.ledger.entries()[key])
        self.fallback.record(key, {k: v for k, v in entry.items() if k not in ("id", "attempts")}, "")
        self.assertEqual(gate_failures.sweep(self.fallback, "[widgets]",
                                             gate_failures.SCRIPTS_DIR, cooldown_s=3600), [])
        self.assertIn("duplicate", self.fallback.entries()[key]["resolution"])

    def test_a_failure_after_a_recovery_that_could_not_settle_home_is_its_own_episode(self):
        self.fail()
        (key,) = self.ledger.entries()
        first = gate_failures.sweep(self.ledger, "[widgets]", gate_failures.SCRIPTS_DIR,
                                    cooldown_s=3600)
        self.assertEqual(len(first), 1)
        with mock.patch.object(gate_failures, "RECORD_S", 0.2), self.holding_home_lock():
            actions.review(self.loop(), 7)       # the gate finishes; the busy home stays open
        self.assertFalse(self.ledger.entries()[key]["resolved"])
        real = gate_failures.Ledger.record

        def home_busy(ledger, *a, **kw):
            if ledger.path == self.ledger.path:
                raise OSError("busy")
            return real(ledger, *a, **kw)
        time.sleep(0.05)
        with mock.patch.object(gate_failures.Ledger, "record", home_busy):
            self.fail()                          # a new crash lands in the fallback
        said = gate_failures.sweep(self.fallback, "[widgets]", gate_failures.SCRIPTS_DIR,
                                   cooldown_s=3600)
        self.assertEqual(len(said), 1)           # its own alert, not a silent duplicate
        self.assertFalse(self.fallback.entries()[key]["resolved"])

    @contextlib.contextmanager
    def holding_home_lock(self):
        """A real flock on the home ledger, held through another file description."""
        with self.ledger._locked():
            yield

    def test_a_busy_first_ledger_does_not_hang_the_recovery_or_hide_the_fallback(self):
        self.fail()
        (key,) = self.ledger.entries()
        self.fallback.record(key, {**self.ledger.entries()[key]}, "")
        done = threading.Event()

        def recover():
            gate_failures.resolve_in_process("gate_reviewer", self.payload())
            done.set()
        with mock.patch.object(gate_failures, "RECORD_S", 0.4), self.holding_home_lock():
            worker = threading.Thread(target=recover, daemon=True)
            worker.start()
            self.assertTrue(done.wait(5), "resolve_in_process hung on a locked ledger")
            self.assertFalse(self.ledger.entries()[key]["resolved"])   # busy: left open
        self.assertTrue(self.fallback.entries()[key]["resolved"])      # the fallback was tried

    def payload(self):
        return {"action": "ready_for_review", "number": 7,
                "pull_request": t.pr(7), "repository": {"full_name": t.REPO}}

    def test_a_clean_exit_zero_neither_crashes_nor_loses_an_open_failure(self):
        self.fail()
        self.assertEqual(len(self.failures()), 1)
        with mock.patch.object(actions, "_run_reviewer", side_effect=SystemExit(0)):
            result = actions.review(self.loop(), 7)      # must not raise UnboundLocalError
        self.assertIn(result["code"], (0, 1))
        self.assertEqual(self.failures(), [])            # the gate did finish: settled on purpose


class Desktop(Harness):
    def test_the_desktop_still_refuses_review_until_its_control_is_wired(self):
        t.reset(prs={"7": t.pr(7)})
        offer = actions.plan(self.loop(), "review", pr=7)
        self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
        self.assertIn("not wired yet", offer["reason"])


if __name__ == "__main__":
    unittest.main()
