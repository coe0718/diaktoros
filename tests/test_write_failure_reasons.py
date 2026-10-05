"""Broker GitHub writes keep GitHub's status and message (#334)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

from review_loop import broker, broker_ipc, config

LOOP = {"repo": "o/r", "base": "main"}
ERRORS = {
    403: 'HTTP 403 {"message": "You have exceeded a secondary rate limit", "documentation_url": "x"}',
    422: 'HTTP 422 {"message": "Validation Failed", "errors": []}',
}
EXPECT = {403: "HTTP 403: You have exceeded a secondary rate limit",
          422: "HTTP 422: Validation Failed"}


class WriteFailureTests(unittest.TestCase):
    def drive(self, fn):
        for status, error in ERRORS.items():
            with self.subTest(status=status), \
                    mock.patch.object(broker.gh, "fetch", return_value=(None, error)), \
                    mock.patch.object(broker.gh, "log"), \
                    mock.patch.object(config, "seat_login", return_value="rev"):
                with self.assertRaises(broker.BrokerDenied) as ctx:
                    fn()
                self.assertIn(EXPECT[status], str(ctx.exception))
                self.assertEqual(broker_ipc._why(ctx.exception), EXPECT[status])

    def test_review_request(self):
        self.drive(lambda: broker.request_issue_pr_review(LOOP, repo="o/r", pr=3, login="fx"))

    def test_comment(self):
        self.drive(lambda: broker.post_issue_comment(LOOP, repo="o/r", number=3, login="fx",
                                                     body="x"))

    def test_pr_create(self):
        self.drive(lambda: broker.open_issue_pr(LOOP, repo="o/r", number=3, branch="b",
                                                title="t", body="b", login="fx"))

    def test_label(self):
        self.drive(lambda: broker.post_triage(LOOP, repo="o/r", number=3, login="t",
                                              labels=["bug"], body=""))

    def test_secrets_redacted_and_bounded(self):
        error = 'HTTP 403 {"message": "bad ghp_' + "a" * 60 + " " + "z" * 500 + '"}'
        reason = broker.failure_reason(error)
        self.assertNotIn("ghp_aaaa", reason)
        self.assertLessEqual(len(reason), 180)
        self.assertTrue(reason.startswith("HTTP 403: "))

    def test_non_http_error_and_non_broker_exception(self):
        self.assertEqual(broker.failure_reason("gh stub rc=1: boom"), "gh stub rc=1: boom")
        self.assertEqual(broker_ipc._why(ValueError("x")), "ValueError")


if __name__ == "__main__":
    unittest.main()
