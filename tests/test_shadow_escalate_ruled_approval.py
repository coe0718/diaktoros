"""#1706: a shadow escalation stands on the reviewer's own approval, never the adjudicator's.

Since #905 the head's effective verdict can be the adjudicator's approval on its ACCEPT ruling
(``gate.ruled_approvals``) while the reviewer's own verdict there still requests changes. The
shadow escalation's reason and the ruling prompt (``turn_prompts.adjudication_situation``) say
the reviewer approved the head, so that ruled approval sends nothing: not from
``gate.shadows.shadow_escalation``, not from the watchdog sweep, and the adjudicator's claim
check (``pr_facts.adjudication_state``) reads such a shadow escalation as moot.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_ruled_approval as tra  # noqa: E402
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import gate  # noqa: E402

OWN = tra.review(5, "APPROVED")                     # the reviewer's own approval


class RuledApproval(tra.Base):
    def escalate(self, reviews):
        with mock.patch.object(gate, "breach", return_value="new") as breach:
            outcome = gate.shadows.shadow_escalation(self.loop, self.st, 7, fg.HEAD, reviews,
                                                     db=self.db)
        return outcome, breach

    def test_a_ruled_approval_over_a_change_request_sends_nothing(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("ACCEPT")
        # The head's effective verdict is the adjudicator's ruled approval ...
        self.assertIs(self.latest([tra.CHANGES, tra.TUCK]), tra.TUCK)
        # ... but the reviewer did not approve it: no shadow escalation says it did.
        for reviews in ([tra.CHANGES, tra.TUCK], [tra.DISMISSED, tra.TUCK]):
            with self.subTest(reviews=[r["state"] for r in reviews]):
                outcome, breach = self.escalate(reviews)
                self.assertEqual(outcome, "not approved")
                breach.assert_not_called()

    def test_the_reviewers_own_approval_still_goes(self):
        self.shadow("REQUEST_CHANGES")
        outcome, breach = self.escalate([OWN])
        self.assertEqual(outcome, "new")
        breach.assert_called_once()

    def test_the_helper_tells_the_two_apart(self):
        self.assertTrue(gate.reviewer_approved(OWN, self.loop))
        self.assertFalse(gate.reviewer_approved(tra.TUCK, self.loop))
        self.assertFalse(gate.reviewer_approved(tra.CHANGES, self.loop))
        self.assertFalse(gate.reviewer_approved(None, self.loop))

    def test_the_sweep_sends_a_ruled_approval_nowhere(self):
        # Through the enforcing entry point: the watchdog sweep's backstop check.
        self.shadow("REQUEST_CHANGES")
        self.ruled("ACCEPT")
        with mock.patch.object(ser, "APPROVED", [tra.CHANGES, tra.TUCK]), \
                mock.patch.object(gate, "breach", return_value="new") as breach:
            ser.CallSites.sweep(self)                         # first sweep: snapshot only
            ser.CallSites.sweep(self)
        self.assertNotIn((7, fg.HEAD), [c.args[2:4] for c in breach.call_args_list])

    def test_the_claim_reads_a_shadow_escalation_on_a_ruled_approval_as_moot(self):
        # The adjudicator's claim check agrees with the trigger (#1706): a shadow ruling on a
        # head only the adjudicator approved would tell it the reviewer approved.
        from diaktoros import gh
        from diaktoros.run_supervisor import pr_facts
        self.ruled("ACCEPT")
        self.mark_escalated(status="awaiting-adjudication")
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}}
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD, "seat": "adjudicator",
               "turn_key": "breach:1", "id": "r1", "created": time.time()}

        def claim(reviews):
            with mock.patch.object(gh, "api", return_value=pr), \
                    mock.patch.object(gh, "reviews", return_value=list(reviews)), \
                    mock.patch.object(pr_facts, "effective_reviews",
                                      side_effect=lambda loop, row, reviews, db=None: reviews):
                return pr_facts.adjudication_state(self.loop, row)[0]

        self.assertEqual(claim([tra.CHANGES, tra.TUCK]), "superseded")
        self.assertEqual(claim([OWN]), "ok")


if __name__ == "__main__":
    unittest.main()
