"""Broker denial messages (#65)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest

from diaktoros import broker, broker_ipc


class DenialMessageTests(unittest.TestCase):
    def msg(self, exc):
        return broker_ipc._denial_message(exc)

    def test_fixed_validation_reasons_pass_through(self):
        for reason in ("file too large", "manifest base differs from scoped PR head",
                       "unsafe file path", "repository control file", "PR branch moved",
                       "stale PR head"):
            with self.subTest(reason):
                self.assertEqual(self.msg(broker.BrokerDenied(reason)), "write denied: " + reason)

    def test_other_denials_stay_opaque(self):
        for reason in ("missing token for seat-bot", "GitHub write did not return a successful response",
                       "something with ghp_secret", "cannot verify live PR"):
            with self.subTest(reason):
                self.assertEqual(self.msg(broker.BrokerDenied(reason)), "write denied")

    def test_an_unconfirmed_push_never_reads_as_not_published(self):
        """#351: the push landed, the PR lagged, and the seat was told "write denied"."""
        from diaktoros import safe_push
        lagged = self.msg(safe_push.PushFailure("published_pr_unverified"))
        self.assertIn("the branch moved to your commit", lagged)
        self.assertIn("do not say it was not published", lagged)
        for outcome in ("unknown", "unchanged", "anything-else"):
            with self.subTest(outcome):
                self.assertEqual(self.msg(safe_push.PushFailure(outcome)), broker_ipc.PUSH_UNKNOWN)
        self.assertNotIn("write denied", lagged)

    def test_a_published_push_is_reported_as_published_not_unknown(self):
        """#824: the transport errored, the ref read back at the new head; the audit says published."""
        from diaktoros import safe_push
        text = self.msg(safe_push.PushFailure("published"))
        self.assertNotEqual(text, broker_ipc.PUSH_UNKNOWN)
        self.assertTrue(text.startswith("push published"))
        self.assertIn("do not say its outcome is unknown", text)
        self.assertNotIn("could not confirm whether the branch moved", text)
        self.assertEqual(safe_push.held_outcome(safe_push.PushFailure("published")), "published")
        self.assertEqual(safe_push.held_outcome(safe_push.PushFailure("unchanged")), "unknown")
        self.assertEqual(safe_push.held_outcome(RuntimeError("x")), "unknown")

    def test_a_review_that_posted_never_reads_as_not_published(self):
        """#825: the POST succeeded, then the PR read back ineligible or the readback failed."""
        from diaktoros import review_receipt
        posted = self.msg(review_receipt.ReviewedPrIneligible("closed"))
        self.assertIn("review posted, but the PR is now closed", posted)
        unconfirmed = self.msg(review_receipt.ReviewPostUnconfirmed("exact-ID readback mismatch"))
        self.assertIn("exact-ID readback mismatch", unconfirmed)
        for text in (posted, unconfirmed):
            self.assertNotIn("write denied", text)
            self.assertIn("do not say it was not published", text)
        # A refusal made before the POST stays opaque.
        self.assertEqual(self.msg(review_receipt.ReceiptDenied("generation changed before POST")),
                         "write denied")

    def test_every_public_reason_is_a_known_constant_string(self):
        for reason in broker_ipc.PUBLIC_DENIALS:
            self.assertNotIn("{", reason)


if __name__ == "__main__":
    unittest.main()
