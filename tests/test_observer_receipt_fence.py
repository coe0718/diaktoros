"""#2629 review F2: a receipt from a claim that was taken over writes nothing on the claim that
replaced it. The posting fence stops a late sender before its POST; this one stops a sender
whose POST already began, was quarantined as stale, settled by a person and claimed again."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import threading
import unittest
from unittest.mock import patch

from diaktoros import observer, routes

import _ledger_guard  # noqa: E402
from test_observer_resend import Fixture  # noqa: E402

setUpModule, tearDownModule = _ledger_guard.module_home()


class StaleReceiptTests(Fixture):
    def test_a_quarantined_senders_receipt_leaves_the_reclaimed_notice_alone(self):
        key = self.failed_notice()
        self.fix_route()
        released, posting, seen = threading.Event(), threading.Event(), []
        late = threading.Thread(target=observer.resend, args=(self.loop, self.state, key))

        def post(*_args, **_kwargs):
            if threading.current_thread() is late:
                posting.set()                         # its POST began: posting_at is marked
                released.wait(10)
                return True                           # the old POST's receipt arrives late
            released.set()
            late.join(10)
            entry = self.entries()[key]
            seen.append((entry["status"], entry["claim"]))
            return True
        with patch.object(routes, "fire", side_effect=post) as fire:
            late.start()
            self.assertTrue(posting.wait(10))
            self.age(key)
            self.assertEqual(observer.retry(self.loop, self.state), 0)     # quarantined
            self.assertEqual(self.entries()[key]["status"], "uncertain")
            observer.settle(self.state, key, delivered=False)
            self.assertEqual(observer.retry(self.loop, self.state), 1)     # claimed again
        self.assertFalse(late.is_alive())
        self.assertEqual(fire.call_count, 2)
        entry = self.entries()[key]
        self.assertEqual(seen, [("pending", entry["claim"])],
                         "the old claim's receipt must not settle the new claim")
        self.assertEqual((entry["status"], entry["attempts"]), ("delivered", observer.MAX_ATTEMPTS))

    def test_a_stale_receipt_leaves_a_reclaimed_digest_and_its_members_alone(self):
        self.loop["observer"]["digest_min"] = 30
        for number in (7, 8):
            observer.notify(self.loop, self.state, "opened", number, "a" * 40)
        self.assertFalse(observer.flush(self.loop, self.state, wait_s=0))
        [digest] = [k for k, e in self.entries().items() if e.get("batch")]
        members, seen = self.entries()[digest]["batch"], []
        self.fix_route()

        def post(*_args, **_kwargs):
            entries = self.entries()
            seen.append([entries[k]["status"] for k in (digest, *members)])
            for delivered in (False, True):
                observer._receipt(self.state, digest, "a-claim-taken-over", delivered)
            entries = self.entries()
            seen.append([entries[k]["status"] for k in (digest, *members)])
            return True
        with patch.object(routes, "fire", side_effect=post):
            self.assertEqual(observer.retry(self.loop, self.state), 1)
        self.assertEqual(seen[0][0], "pending")
        self.assertEqual(seen[1], seen[0], "a stale receipt must change neither claim nor members")
        entries = self.entries()
        self.assertEqual([entries[k]["status"] for k in (digest, *members)], ["delivered"] * 3)


if __name__ == "__main__":
    unittest.main()
