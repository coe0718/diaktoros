"""Offline host action contracts: snapshot confirmation, gate reuse and honest read-back."""
import _home_guard  # noqa: F401
import copy
import json
import unittest
from unittest import mock

import test_escalate as fixtures
from diaktoros import gate, gh, one_shot_action as actions


class ConfirmedActions(fixtures.Base):
    def test_preview_is_read_only_and_token_is_opaque(self):
        with self.github() as wake, mock.patch.object(gate, "breach") as breach:
            preview = actions.plan(self.loop, "escalate", pr=7)
        self.assertTrue(preview["eligible"])
        self.assertEqual(preview["head"], fixtures.HEAD)
        self.assertEqual(len(preview["confirmation"]), 64)
        self.assertNotIn("/", json.dumps(actions.public_plan(preview)))
        breach.assert_not_called()
        wake.assert_not_called()
        self.assertEqual(self.st.breach_get(7), {})

    def test_moved_head_refuses_without_mutation(self):
        with self.github(), mock.patch.object(gate, "breach") as breach:
            preview = actions.plan(self.loop, "escalate", pr=7)
            self.live = copy.deepcopy(self.live)
            self.live["head"]["sha"] = fixtures.OTHER
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "stale")
        breach.assert_not_called()

    def test_different_target_or_action_cannot_reuse_confirmation(self):
        with self.github(), mock.patch.object(gate, "breach") as breach:
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "review", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        breach.assert_not_called()

    def test_escalation_reads_back_the_exact_marker(self):
        with self.github():
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(gate.escalated(self.st.breach_get(7), fixtures.HEAD))

    def test_delivery_pending_is_partial_not_success(self):
        with self.github(), mock.patch.object(gate, "wake_adjudicator", return_value=False):
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")

    def test_approved_escalation_is_ineligible(self):
        self.reviews = [fixtures.verdict(state="APPROVED")]
        with self.github():
            preview = actions.plan(self.loop, "escalate", pr=7)
        self.assertFalse(preview["eligible"])
        self.assertIn("approved", preview["reason"])

    def test_unreadable_pr_has_no_control_and_no_raw_error(self):
        with mock.patch.object(gh, "pr", side_effect=RuntimeError("/secret/token password")):
            preview = actions.plan(self.loop, "review", pr=7)
        self.assertFalse(preview["eligible"])
        self.assertNotIn("secret", json.dumps(actions.public_plan(preview)))

    def test_drain_refuses_unbound_queue_without_calling_watchdog(self):
        with mock.patch.object(actions, "perform_drain") as drain:
            preview = actions.plan(self.loop, "drain", seat="fixer")
            result = actions.confirmed_perform(self.loop, "drain", preview.get("confirmation"), seat="fixer")
        self.assertFalse(preview["eligible"])
        self.assertFalse(result["ok"])
        self.assertIn("snapshot", preview["reason"])
        drain.assert_not_called()

    def test_review_uses_existing_gate_with_confirmed_head(self):
        self.reviews = []
        with self.github(), mock.patch.object(actions, "_run_reviewer") as reviewer:
            result = actions.perform_review(self.loop, 7)
        reviewer.assert_called_once()
        self.assertEqual(reviewer.call_args.args[0]["pull_request"]["head"]["sha"], fixtures.HEAD)
        self.assertFalse(result["ok"])

    def test_head_moving_after_confirmation_revalidation_never_escalates_new_head(self):
        with self.github():
            preview = actions.plan(self.loop, "escalate", pr=7)
            moved = copy.deepcopy(self.live)
            moved["head"]["sha"] = fixtures.OTHER
            with mock.patch.object(gh, "pr", side_effect=[self.live, moved]), \
                    mock.patch.object(gate, "breach") as breach:
                result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        breach.assert_not_called()

    def test_changed_verdicts_invalidate_confirmation(self):
        with self.github(), mock.patch.object(gate, "breach") as breach:
            preview = actions.plan(self.loop, "escalate", pr=7)
            self.reviews.append(fixtures.verdict(rid=6))
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertEqual(result["status"], "stale")
        breach.assert_not_called()

    def test_bad_marker_store_never_offers_a_control(self):
        self.st.breach.parent.mkdir(parents=True, exist_ok=True)
        self.st.breach.write_text("corrupt /host/secret")
        with self.github(), mock.patch.object(gate, "breach") as breach:
            preview = actions.plan(self.loop, "escalate", pr=7)
        self.assertFalse(preview["eligible"])
        self.assertNotIn("secret", json.dumps(preview))
        breach.assert_not_called()

    def test_breach_return_without_readback_is_not_success(self):
        with self.github(), mock.patch.object(gate, "breach", return_value="new"):
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")

    def test_exception_after_write_is_unknown_and_does_not_leak(self):
        def interrupted(*args, **kwargs):
            raise RuntimeError("/host/secret token")
        with self.github(), mock.patch.object(gate, "breach", side_effect=interrupted):
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("secret", json.dumps(result))

    def test_review_preview_never_mutates_process_globals_or_calls_gate(self):
        with self.github(), mock.patch.object(actions, "_run_reviewer") as reviewer:
            preview = actions.plan(self.loop, "review", pr=7)
            result = actions.confirmed_perform(self.loop, "review", "x" * 64, pr=7)
        self.assertFalse(preview["eligible"])
        self.assertFalse(result["ok"])
        reviewer.assert_not_called()

    def test_cli_gate_timeout_remains_unknown_not_success(self):
        with self.github(), mock.patch.object(actions, "_run_reviewer", side_effect=gh.GateBudgetExceeded("timeout")):
            result = actions.perform_review(self.loop, 7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 2)
        self.assertIn("no answer", " ".join(result["lines"]))

    def test_same_head_verdict_change_after_outer_revalidation_never_mutates(self):
        with self.github() as wake:
            preview = actions.plan(self.loop, "escalate", pr=7)
            old = copy.deepcopy(self.reviews)
            with mock.patch.object(gh, "reviews", side_effect=[old, old + [fixtures.verdict(rid=6)]]), \
                    mock.patch.object(gate, "breach") as breach:
                result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"])
        breach.assert_not_called()
        wake.assert_not_called()
        self.assertEqual(self.st.breach_get(7), {})

    def test_same_head_facts_change_inside_breach_lock_never_reserves(self):
        for change in ("reviews", "config", "marker", "ledger"):
            with self.subTest(change=change):
                self.configure(adjudicator=True)
                self.reviews = [fixtures.verdict()]
                self.st.breach.unlink(missing_ok=True)
                with self.github() as wake:
                    preview = actions.plan(self.loop, "escalate", pr=7)
                    original = gate.breach
                    def race(*args, **kwargs):
                        changed = False
                        def current_pr(*a, **k):
                            nonlocal changed
                            # gate.breach calls this only from its locked current-head check.
                            if not changed:
                                changed = True
                                if change == "reviews":
                                    self.reviews.append(fixtures.verdict(rid=6))
                                elif change == "config":
                                    path = actions.config.config_dir() / "one.json"
                                    data = json.loads(path.read_text())
                                    data["cap"] = 99
                                    path.write_text(json.dumps(data))
                                elif change == "marker":
                                    self.st.breach.parent.mkdir(parents=True, exist_ok=True)
                                    self.st.breach.write_text("corrupt")
                            return self.live
                        with mock.patch.object(gh, "pr", side_effect=current_pr):
                            return original(*args, **kwargs)
                    rows = []
                    calls = 0
                    def ledger_rows(*args):
                        nonlocal calls
                        if change == "ledger" and calls == 2:
                            rows.append(("changed", "reviewer", fixtures.HEAD, "finished", "now"))
                        calls += 1
                        return list(rows)
                    with mock.patch.object(gate, "breach", side_effect=race), \
                            mock.patch.object(actions, "_ledger_rows", side_effect=ledger_rows):
                        result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
                self.assertFalse(result["ok"], result)
                wake.assert_not_called()
                if change != "marker":
                    self.assertEqual(self.st.breach_get(7), {})

    def test_cli_drain_keeps_its_documented_exit_code_and_states_the_caveat(self):
        # docs/commands.md: drain propagates its script's code and does not wait for completion,
        # so a completed sweep is 0. Returning 2 here would claim a refusal that never happened.
        watchdog = mock.Mock(WATCHDOG_BUDGET_S=30, WATCHDOG_PER_CALL_S=2)
        before = dict(gh._GATE)
        with mock.patch.object(actions, "_script", return_value=watchdog):
            result = actions.perform_drain(self.loop, "fixer")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["code"], 0)
        self.assertIn("unconfirmed", " ".join(result["lines"]))
        watchdog.run.assert_called_once()
        self.assertEqual(watchdog.run.call_args.args[0].seat, "fixer")
        self.assertEqual(gh._GATE, before)

    def test_cli_drain_reports_a_refused_loop_rather_than_success(self):
        watchdog = mock.Mock(WATCHDOG_BUDGET_S=30, WATCHDOG_PER_CALL_S=2,
                             run=mock.Mock(side_effect=SystemExit(1)))
        with mock.patch.object(actions, "_script", return_value=watchdog):
            result = actions.perform_drain(self.loop, "reviewer")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 1)

    def test_cli_drain_budget_exhaustion_is_not_success(self):
        watchdog = mock.Mock(WATCHDOG_BUDGET_S=30, WATCHDOG_PER_CALL_S=2,
                             run=mock.Mock(side_effect=gh.GateBudgetExceeded("budget")))
        with mock.patch.object(actions, "_script", return_value=watchdog):
            result = actions.perform_drain(self.loop, "reviewer")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], 2)

    def test_plan_and_cli_use_the_same_escalation_selector(self):
        with self.github(), mock.patch.object(actions, "_escalation_decision", wraps=actions._escalation_decision) as select:
            preview = actions.plan(self.loop, "escalate", pr=7)
            self.assertTrue(preview["eligible"])
            result = actions.perform_escalate(self.loop, 7)
        self.assertTrue(result["ok"], result)
        self.assertEqual(select.call_count, 2)

    def test_other_operation_marker_at_same_head_is_not_success(self):
        original = gate.breach
        def replaced(*args, **kwargs):
            outcome = original(*args, **kwargs)
            data = json.loads(self.st.breach.read_text())
            data[gate.seat_key(self.loop, 7)]["escalated"]["reason"] = "another operation"
            self.st.breach.write_text(json.dumps(data))
            return outcome
        with self.github(), mock.patch.object(gate, "breach", side_effect=replaced):
            preview = actions.plan(self.loop, "escalate", pr=7)
            result = actions.confirmed_perform(self.loop, "escalate", preview["confirmation"], pr=7)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "unknown")

    def test_the_reviewer_gate_runs_as_a_call_and_changes_nothing_process_wide(self):
        # #1046: no stdin/stdout swap, no signal handler or timer, and the gate's own budget,
        # failed-read flag and decision recorder are the call's — even when the gate sets them.
        import signal
        import sys
        from diaktoros import util
        stdin, stdout = sys.stdin, sys.stdout
        recorder = util._DECISION_RECORDER
        budget = dict(gh._GATE)
        failures = gh._RECORD_FAILURES
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGALRM, signal.SIGTERM)}
        timer = signal.getitimer(signal.ITIMER_REAL)
        seen = {}
        entry = mock.Mock()
        def decide(payload, reconsider=None):
            seen.update(payload=payload, reconsider=reconsider, stdin=sys.stdin, stdout=sys.stdout,
                        deadline=gh.remaining() is not None)
            gh.record_failures()
            util.set_decision_recorder(object())
            gh.begin_gate(0.0)
            util.log("a reason")
            raise TimeoutError("gate alarm")
        entry.decide = decide
        with mock.patch.object(actions, "_script", return_value=entry):
            with self.assertRaises(TimeoutError):
                actions._run_reviewer({"number": 7}, reconsider="why")
        self.assertEqual(seen, {"payload": {"number": 7}, "reconsider": "why", "stdin": stdin,
                                "stdout": stdout, "deadline": True})
        self.assertIs(util._DECISION_RECORDER, recorder)
        self.assertEqual(gh._GATE, budget)
        self.assertEqual(gh._RECORD_FAILURES, failures)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), timer)
        self.assertEqual({sig: signal.getsignal(sig) for sig in handlers}, handlers)

    def test_unknown_outcome_and_host_paths_never_become_offers(self):
        from diaktoros import ledger, run_supervisor
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
        with mock.patch.object(sup, '_spawn'):
            sup.enqueue('unknown-target', self.loop['repo'], 7, fixtures.HEAD, 'reviewer')
        for state in ('uncertain', 'quarantined'):
            with self.subTest(state=state), ledger.connect(sup.db) as con:
                con.execute('UPDATE runs SET state=?', (state,))
            with self.github() as wake:
                preview = actions.plan(self.loop, 'escalate', pr=7)
            self.assertFalse(preview['eligible'])
            self.assertEqual(preview['confirmation'], '')
            wake.assert_not_called()
        unsafe = {**self.loop, 'base': '/private/PAT ghp_credential' * 100}
        with self.github():
            preview = actions.public_plan(actions.plan(unsafe, 'escalate', pr=7))
        self.assertFalse(preview['eligible'])
        self.assertNotIn('/private', json.dumps(preview))
        self.assertNotIn('ghp_', json.dumps(preview))
        self.assertLess(len(preview['reason']), 200)

    def test_cli_grant_and_drain_seat_forward_unchanged(self):
        with mock.patch.object(actions, "_review", return_value=0) as review:
            actions.perform_review(self.loop, 7, another_round=True)
        self.assertTrue(review.call_args.args[1].another_round)
        with mock.patch.object(actions, "_drain", return_value=0) as drain:
            actions.perform_drain(self.loop, "fixer")
        self.assertEqual(drain.call_args.args, (self.loop, "fixer"))


if __name__ == "__main__":
    unittest.main()
