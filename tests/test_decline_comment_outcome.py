"""#2036: ``explain`` says when a decline's PR comment may not have landed.

A CI fix's or a conflict turn's decline posts its reason as the fixer's answers comment. A POST
whose outcome is unknown ('uncertain', or 'posting' when its outcome was never written) still ends
the run as declined, and nothing retries it; ``explain`` named the decline as if its comment were
on the PR. It now reads the answers record the broker keeps for the run and says the outcome is
unknown, so a maintainer checks the PR for the reason.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import dataclasses
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_answers as base  # noqa: E402
from diaktoros import explain, observer  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

WHY = "The runner was shut down mid-job; nothing in this PR's code fails.\n"
UNKNOWN = "its PR comment's POST outcome is unknown"


class DeclineOutcome(base.Base):
    def setUp(self):
        super().setUp()
        self.scope = dataclasses.replace(self.scope, ci_fix=True)
        patch = mock.patch.object(observer, "notify")
        patch.start()
        self.addCleanup(patch.stop)

    def decline(self, target=None, name="", error=None):
        """The decline through the real broker, with ``target.name`` raising ``error`` if given."""
        with (mock.patch.object(target, name, side_effect=error) if target
              else contextlib.nullcontext()), self.serving() as server:
            return self.send(server, {"operation": "request_review", "body": WHY,
                                      "verdict": "DECLINE:not-this-pr"})["result"]["answers"]

    def explained(self):
        """``explain``'s blockers for #7 at HEAD, read from this test's ledger."""
        c = types.SimpleNamespace(pr={}, state="open", repo=base.REPO, number=7, head=base.HEAD,
                                  short=base.HEAD[:7], blockers=[])
        with mock.patch.object(explain.config, "host_path", return_value=self.db), \
                mock.patch.multiple(explain, _read_blockers=mock.DEFAULT,
                                    _gate_blockers=mock.DEFAULT, _run_blockers=mock.DEFAULT):
            explain._blockers(c)
        return c.blockers

    def test_a_posted_decline_names_no_unknown_outcome(self):
        self.assertEqual(self.decline(), "posted")
        (line,) = self.explained()
        self.assertIn("declined (not-this-pr)", line)
        self.assertNotIn(UNKNOWN, line)

    def test_a_decline_whose_post_was_lost_says_its_outcome_is_unknown(self):
        lost = RuntimeError("connection reset")
        self.assertEqual(self.decline(base.broker_ipc.broker, "post_fixer_answers", lost),
                         "uncertain")
        (line,) = self.explained()
        self.assertIn("declined (not-this-pr)", line)
        self.assertIn(f"{UNKNOWN} (answers uncertain), so check the PR for the reason", line)
        self.assertIn("nothing retries it", line)

    def test_a_decline_whose_outcome_was_never_written_says_so_too(self):
        # The POST went out but its outcome could not be recorded: the row stays 'posting'.
        busy = RuntimeError("ledger busy")
        self.assertEqual(self.decline(Supervisor, "answers_status", busy), "posted")
        (line,) = self.explained()
        self.assertIn(f"{UNKNOWN} (answers posting)", line)


if __name__ == "__main__":
    unittest.main()
