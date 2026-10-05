import unittest

from review_loop import broker, broker_ipc


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

    def test_every_public_reason_is_a_known_constant_string(self):
        for reason in broker_ipc.PUBLIC_DENIALS:
            self.assertNotIn("{", reason)


if __name__ == "__main__":
    unittest.main()
