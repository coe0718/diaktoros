"""The Desktop Retry's refusals, on real isolated ledgers (the ``test_desktop_retry`` fixture).

#2406: a refusal line reaches the page as a 409 body, so a malformed recorded head is withheld
there by the same display rule as every read — never cut to 7 characters.
#2469: an issue_fixer row is not offered while issue fixes are off, because the host's retry
refuses it; the page's reason is the host's own refusal.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from unittest import mock

from diaktoros import config, desktop_api, retry_action
from diaktoros.state import ledger
from test_desktop_retry import HEAD, MALFORMED, Fixture


class RefusalsWithholdAMalformedHead(Fixture):
    def corrupt(self, run_id):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET head=? WHERE id=?", (MALFORMED, run_id))

    def refusal(self, **kwargs):
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry(**kwargs)
        return str(caught.exception)

    def test_a_stale_confirmation_never_names_a_malformed_head(self):
        row = self.row("failed")
        self.corrupt(row["id"])
        text = self.refusal(head="a" * 40)
        self.assertIn("head moved", text)
        self.assertIn("it is a withheld head now", text)
        self.assertNotIn(MALFORMED[:7], text)
        self.assertNotIn("SECRET", text)
        self.assertEqual(self.state_of(row["id"])["state"], "failed")

    def test_nothing_to_retry_never_names_a_malformed_head(self):
        row = self.row("succeeded")
        self.corrupt(row["id"])
        text = self.refusal(head="a" * 40)
        self.assertIn("#356 @ a withheld head: nothing to retry", text)
        self.assertNotIn(MALFORMED[:7], text)

    def test_a_well_formed_head_is_still_named(self):
        self.row("failed", head="b" * 40)
        self.assertIn("(it is bbbbbbb now)", self.refusal(head=HEAD))
        self.row("succeeded", seat="fixer", pr=357)
        self.assertIn("#357 @ aaaaaaa: nothing to retry", self.refusal(pr=357, seat="fixer"))


class IssueFixerRetryFollowsTheIssueFixPolicy(Fixture):
    """The fixture loop has no triage seat, so issue fixes are off."""

    def test_a_failed_issue_fixer_row_is_not_offered_while_issue_fixes_are_off(self):
        row = self.row("failed", seat="issue_fixer", pr=30)
        [run] = self.now()["runs"]
        self.assertFalse(run["retry"]["eligible"])
        self.assertEqual(run["retry"]["head"], HEAD)
        self.assertIn("issue fixes are off", run["retry"]["reason"])
        # The page's reason is the host's own refusal, word for word.
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry(pr=30, seat="issue_fixer")
        self.assertIn(run["retry"]["reason"], str(caught.exception))
        self.assertEqual(self.state_of(row["id"])["state"], "failed")

    def test_it_is_offered_once_issue_fixes_are_on(self):
        self.row("failed", seat="issue_fixer", pr=30)
        with mock.patch.object(config, "issue_fixes_off", return_value=()):
            [run] = self.now()["runs"]
        self.assertEqual(run["retry"], {"eligible": True, "head": HEAD, "reason": "",
                                        "target": "head"})

    def test_an_unusable_loop_config_withholds_the_button(self):
        with mock.patch.object(config, "issue_fixes_off", side_effect=KeyError("triage")):
            self.assertIn("unusable", retry_action.issue_fix_policy(self.loop))
