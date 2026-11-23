"""Observer resends: a re-claim starts a fresh attempt (#1968), and a digest whose last allowed
attempt failed before its POST is left settleable, not stuck `queued` (#1970)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from diaktoros import observer, routes
from diaktoros.state import LoopState

import _ledger_guard  # noqa: E402

setUpModule, tearDownModule = _ledger_guard.module_home()


class Fixture(unittest.TestCase):
    """A loop whose observer route fails every POST until ``fix_route`` (shared, #2629 F2)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        subs = pathlib.Path(self.temp.name) / "subscriptions.json"
        env = patch.dict("os.environ", {"DIAKTOROS_SUBS": str(subs)})
        env.start()
        self.addCleanup(env.stop)
        # No loop host: the route has no trusted target, so every POST fails before it begins.
        self.loop = {"id": "private", "repo": "owner/private", "state_dir": self.temp.name,
                     "host": "", "observer": {"route": "private-observe", "profile": "default",
                                               "deliver": "telegram"}}
        subs.write_text(json.dumps({"private-observe": {
            "host": "https://registry.example", "secret": "secret",
            **observer.route_contract(self.loop)}}))
        self.state = LoopState(self.loop)

    def fix_route(self):
        self.loop["host"] = "https://owner.example"

    def entries(self):
        return observer._read(self.state.observations)["entries"]

    def age(self, key):
        data = observer._read(self.state.observations)
        data["entries"][key]["at"] = time.time() - observer.STALE_CLAIM_S - 5
        observer._save(self.state.observations, data)

    def failed_notice(self):
        """A notice whose POST failed before it began, as a real sender leaves it."""
        observer.notify(self.loop, self.state, "opened", 7, "a" * 40)
        [(key, entry)] = list(self.entries().items())
        self.assertEqual((entry["status"], entry["retryable"]), ("failed", True))
        self.assertTrue(entry.get("posting_at"), "the failed attempt marked its POST")
        return key


class ObserverResendTests(Fixture):
    def test_a_retryable_failure_reclaimed_and_killed_is_resent(self):
        # #1968: the sweep's re-claim must not keep the failed attempt's `posting_at`, or a sweep
        # killed before its own mark leaves a claim the next sweep can only quarantine.
        key = self.failed_notice()
        self.fix_route()
        with patch.object(observer, "_mark_posting", side_effect=SystemExit(1)), \
                self.assertRaises(SystemExit):
            observer.retry(self.loop, self.state)
        entry = self.entries()[key]
        self.assertEqual((entry["status"], entry.get("posting_at")), ("pending", None))
        self.age(key)
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()
        self.assertEqual(self.entries()[key]["status"], "delivered")

    def test_a_manual_resend_killed_before_its_post_is_resent_by_the_sweep(self):
        # #1968: `resend` re-claims through the same path.
        key = self.failed_notice()
        self.fix_route()
        with patch.object(observer, "_mark_posting", side_effect=SystemExit(1)), \
                self.assertRaises(SystemExit):
            observer.resend(self.loop, self.state, key)
        self.assertIsNone(self.entries()[key].get("posting_at"))
        self.age(key)
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()

    def capped_manual_resend_killed(self):
        """A failed notice past the sweep's cap whose manual resend died before its POST."""
        key = self.failed_notice()
        data = observer._read(self.state.observations)
        data["entries"][key]["attempts"] = observer.MAX_ATTEMPTS
        observer._save(self.state.observations, data)
        self.fix_route()
        with patch.object(observer, "_mark_posting", side_effect=SystemExit(1)), \
                self.assertRaises(SystemExit):
            observer.resend(self.loop, self.state, key)
        self.age(key)
        return key

    def test_a_capped_manual_resend_killed_before_its_post_is_resent_once(self):
        # #2611: `resend` takes a notice past MAX_ATTEMPTS; killed before its POST, the sweep
        # used to mark it failed and never send it, though the claim never reached the gateway.
        key = self.capped_manual_resend_killed()
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()
        self.assertEqual(self.entries()[key]["status"], "delivered")

    def test_a_capped_manual_resend_is_recovered_by_the_sweep_only_once(self):
        # The sweep's own re-claim is not a manual one: killed too, it stays failed for a person.
        key = self.capped_manual_resend_killed()
        with patch.object(observer, "_mark_posting", side_effect=SystemExit(1)), \
                self.assertRaises(SystemExit):
            observer.retry(self.loop, self.state)
        self.age(key)
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 0)
            fire.assert_not_called()
        self.assertEqual((self.entries()[key]["status"], self.entries()[key]["retryable"]),
                         ("failed", True))
        with patch.object(routes, "fire", return_value=True):
            self.assertTrue(observer.resend(self.loop, self.state, key))

    def test_a_late_manual_sender_writes_nothing_on_the_sweeps_claim(self):
        # #2611 review F1: stale is not dead. A capped manual resend paused before its mark, its
        # claim taken over by the sweep, resumes during the sweep's POST: it must not POST too,
        # nor record its own outcome on the sweep's claim.
        key = self.failed_notice()
        data = observer._read(self.state.observations)
        data["entries"][key]["attempts"] = observer.MAX_ATTEMPTS
        observer._save(self.state.observations, data)
        self.fix_route()
        real, paused, go = observer._mark_posting, threading.Event(), threading.Event()
        late = threading.Thread(target=observer.resend, args=(self.loop, self.state, key))

        def mark(*args):
            if threading.current_thread() is late and not go.is_set():
                paused.set()
                go.wait(10)
            return real(*args)

        def post(*_args, **_kwargs):
            if threading.current_thread() is not late and not go.is_set():
                go.set()                                  # the late sender resumes mid-POST
                late.join(10)
            return True
        with patch.object(observer, "_mark_posting", side_effect=mark), \
                patch.object(routes, "fire", side_effect=post) as fire:
            late.start()
            self.assertTrue(paused.wait(10))
            self.age(key)
            self.assertEqual(observer.retry(self.loop, self.state), 1)
        self.assertFalse(late.is_alive())
        self.assertEqual(fire.call_count, 1)                  # the sweep's POST, not a second
        self.assertEqual(self.entries()[key]["status"], "delivered")

    def digest_failed_to_the_cap(self):
        """Two queued notices whose digest fails before its POST on every allowed attempt."""
        self.loop["observer"]["digest_min"] = 30
        for number in (7, 8):
            observer.notify(self.loop, self.state, "opened", number, "a" * 40)
        self.assertFalse(observer.flush(self.loop, self.state, wait_s=0))
        for _ in range(observer.MAX_ATTEMPTS - 1):
            self.assertEqual(observer.retry(self.loop, self.state), 0)
        [digest] = [k for k, e in self.entries().items() if e.get("batch")]
        return digest, self.entries()[digest]["batch"]

    def test_a_digest_failed_on_its_last_attempt_is_settleable_and_sent_once(self):
        # #1970: left `failed` at the cap, its members stay `queued` for ever: flush refuses the
        # failed digest key and settle/owed_entries see nothing. The sweep makes it uncertain.
        digest, members = self.digest_failed_to_the_cap()
        self.assertEqual(self.entries()[digest]["attempts"], observer.MAX_ATTEMPTS)
        self.fix_route()
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 0)
            self.assertFalse(observer.flush(self.loop, self.state, wait_s=0))
            fire.assert_not_called()
        entries = self.entries()
        self.assertEqual([entries[k]["status"] for k in (digest, *members)],
                         ["uncertain"] * 3)
        self.assertIn("last attempt", entries[digest]["error"])
        self.assertIn(digest, [k for k, _ in observer.owed_entries(self.state)])
        observer.settle(self.state, digest, delivered=False)
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()
        entries = self.entries()
        self.assertEqual([entries[k]["status"] for k in (digest, *members)],
                         ["delivered"] * 3)
        self.assertEqual(observer.unsettled(self.state), 0)

    def test_a_settled_digest_whose_one_resend_fails_is_settleable_again(self):
        # #1970's second path: the operator's one allowed resend also fails before its POST.
        digest, members = self.digest_failed_to_the_cap()
        observer.retry(self.loop, self.state)
        observer.settle(self.state, digest, delivered=False)
        self.assertEqual(observer.retry(self.loop, self.state), 0)   # route still broken
        self.assertEqual(self.entries()[digest]["status"], "failed")
        observer.retry(self.loop, self.state)
        self.assertIn(digest, [k for k, _ in observer.owed_entries(self.state)])
        self.assertEqual({self.entries()[m]["status"] for m in members}, {"uncertain"})

    def test_a_digest_below_the_cap_is_still_resent(self):
        # Guard: only the last attempt is quarantined; earlier failures keep retrying.
        self.loop["observer"]["digest_min"] = 30
        observer.notify(self.loop, self.state, "opened", 7, "a" * 40)
        observer.flush(self.loop, self.state, wait_s=0)
        self.fix_route()
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertEqual(observer.retry(self.loop, self.state), 1)
            fire.assert_called_once()
        self.assertEqual({e["status"] for e in self.entries().values()}, {"delivered"})


if __name__ == "__main__":
    unittest.main()
