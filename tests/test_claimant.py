"""#1337: a claim-time read failure names its claimant, and code older than the install refuses
to claim.

Real SQLite ledger; the worker runs in-process and the claim's GitHub reads go through a
patched ``gh.api``. The loaded and installed plugin commits are patched, never read from a
reinstall.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import sys
import unittest
from unittest.mock import patch

from diaktoros import claimant
import test_retryable_turns as turns

OLD, NEW = "1" * 40, "2" * 40


class ClaimantLine(unittest.TestCase):
    def test_identity_names_pid_executable_and_plugin_commit(self):
        with patch.object(claimant, "LOADED", OLD):
            line = claimant.identity(installed=OLD)
        self.assertIn(f"pid {os.getpid()}", line)
        self.assertIn(sys.executable, line)
        self.assertIn(f"plugin {OLD[:7]}", line)
        self.assertNotIn("installed", line, "one commit when loaded and installed agree")

    def test_identity_adds_the_installed_commit_when_it_differs(self):
        with patch.object(claimant, "LOADED", OLD):
            self.assertIn(f"plugin {OLD[:7]}, installed {NEW[:7]}", claimant.identity(installed=NEW))

    def test_stale_only_when_both_commits_are_known_and_differ(self):
        with patch.object(claimant, "LOADED", OLD):
            self.assertIn(f"loaded plugin {OLD[:7]}, but {NEW[:7]} is installed",
                          claimant.stale(installed=NEW))
            self.assertEqual(claimant.stale(installed=OLD), "")
            self.assertEqual(claimant.stale(installed=""), "", "an unreadable install never refuses")
        with patch.object(claimant, "LOADED", ""):
            self.assertEqual(claimant.stale(installed=NEW), "", "an unknown load never refuses")

    def test_this_checkout_reads_its_own_commit(self):
        self.assertEqual(claimant.installed_commit(), claimant.LOADED)


class ClaimantAtClaim(turns.Base):
    """The claim-time read set-up of ``test_retryable_turns.ClaimTimeReads``, without its tests."""
    LOOP = turns.ClaimTimeReads.LOOP
    pull = turns.ClaimTimeReads.pull
    claim = turns.ClaimTimeReads.claim

    def setUp(self):
        super().setUp()
        self.LOOP = {**self.LOOP, 'state_dir': str(self.root / 'state')}
        self.s = self.sup(production_claim=True)
        self.s.submit('review', 'o/r', 1, turns.HEAD, 'reviewer')

    def test_a_claim_read_failure_records_the_claimant(self):
        with patch.object(claimant, "LOADED", OLD), \
             patch.object(claimant, "installed_commit", return_value=OLD):
            self.assertIsNone(self.claim(None))       # gh.api answers None: the read failed
        error = self.s.get("review")["error"]
        self.assertIn("claim-time read failed: PR unreadable", error)
        self.assertIn(f"[claimant pid {os.getpid()} {sys.executable}", error)
        self.assertIn(f"plugin {OLD[:7]}]", error)

    def test_a_claimant_older_than_the_install_refuses_and_leaves_the_run_pending(self):
        with patch.object(claimant, "LOADED", OLD), \
             patch.object(claimant, "installed_commit", return_value=NEW), \
             patch("diaktoros.util.log") as log:
            self.assertIsNone(self.claim(self.pull()))
        row = self.s.get("review")
        self.assertEqual((row["state"], row["attempts"], row["retries"]), ("pending", 0, 0))
        self.assertIn(f"claim refused: this process loaded plugin {OLD[:7]}", log.call_args[0][0])
        # A worker on the installed code claims the same run.
        with patch.object(claimant, "LOADED", NEW), \
             patch.object(claimant, "installed_commit", return_value=NEW):
            claimed = self.claim(self.pull())
        self.assertIsNotNone(claimed)
        self.assertEqual(self.s.get("review")["state"], "claimed")
        self.assertEqual(self.s.get("review")["head"], turns.HEAD)


if __name__ == "__main__":
    unittest.main()
