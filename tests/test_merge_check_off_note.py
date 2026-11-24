"""``apply`` and ``set`` warn when their change stops the host posting the merge check (#1987),
and name the setting that stopped it as it is (#1986).

A ruleset ``protect`` wrote still requires ``diaktoros/review`` once posting is off, so every PR
stays blocked until posting is back on or the ruleset is removed. Before #1987 only ``protect``
and ``doctor`` said so: ``apply`` was silent when the reviewer moved to another login of the same
App (the pin holds, so the pin note is rightly skipped) while turning the check off, and ``set
--merge-check off`` never warned. #1986: apply called an explicit ``merge_check: off`` ``auto``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import gh  # noqa: E402

APP_ID, BOT_REV, BOT_TWO = 5416187, "acme-reviewer[bot]", "acme-reviewer-two[bot]"
OFF_WARNING = ("If a ruleset `protect` wrote still requires it, every PR stays blocked until you "
               "turn posting back on (`hermes dk set --loop widgets --merge-check on`) or remove "
               "the ruleset (`hermes dk protect --loop widgets --remove`)")


class OffWarning(app_cli.Base):
    def setUp(self):
        app_cli.Base.setUp(self)
        rc, out = self.run_cli(self.app_argv(reviewer=tfs.REV,
                                             tokens=[(tfs.READER, self.pats["read"]),
                                                     (tfs.REV, self.pats["rev"]),
                                                     (tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)
        patch = mock.patch.object(gh, "principal",
                                  side_effect=lambda loop, login, fetch=None: {"login": login,
                                                                               "id": 4242})
        patch.start()
        self.addCleanup(patch.stop)

    def reviewer_on(self, login, app_id=APP_ID):
        rc, out = self.run_cli(["identity", "--loop", "widgets", "--seat", "reviewer", "--app",
                                f"{login}={app_id}:{self.key(login[:12])}"])
        self.assertEqual(rc, 0, out)

    def two_logins_on_one_app(self):
        """The reviewer is BOT_REV on the App; BOT_TWO is mapped to the same App id."""
        self.reviewer_on(BOT_TWO)
        self.reviewer_on(BOT_REV)
        self.assertEqual(self.loop_json()["apps"][BOT_TWO]["app_id"], APP_ID)

    def apply_form(self, *extra, **form):
        return self.run_cli(["apply", "--loop", "widgets", *extra], settings=form)

    def test_a_same_app_move_names_no_pin(self):
        # The exclusion's own test: another login on the same App keeps protect's pin.
        self.two_logins_on_one_app()
        rc, out = self.apply_form("--identity-from-form", reviewer_login=BOT_TWO)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["seats"]["reviewer"]["login"], BOT_TWO, out)
        self.assertNotIn("pinned to", out)
        self.assertNotIn("merge check", out)        # still posted, by the same App

    def test_a_same_app_move_that_turns_the_check_off_warns(self):
        self.two_logins_on_one_app()
        rc, out = self.apply_form("--identity-from-form", reviewer_login=BOT_TWO,
                                  merge_check="off")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["merge_check"], "off", out)
        self.assertIn("the diaktoros/review merge check is now off (merge_check: off).", out)
        self.assertIn(OFF_WARNING, out)
        self.assertNotIn("pinned to", out)

    def test_a_same_app_move_dry_run_says_it_would_stop(self):
        self.two_logins_on_one_app()
        rc, out = self.apply_form("--identity-from-form", "--dry-run", reviewer_login=BOT_TWO,
                                  merge_check="off")
        self.assertIn("merge check would stop being posted (merge_check: off)", out)
        self.assertNotIn("is now off", out)

    def test_apply_turning_the_check_off_without_a_move_warns(self):
        self.reviewer_on(BOT_REV)
        rc, out = self.apply_form(reviewer_login=BOT_REV, merge_check="off")
        self.assertEqual(rc, 0, out)
        self.assertIn(OFF_WARNING, out)

    def test_set_merge_check_off_warns(self):
        self.reviewer_on(BOT_REV)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--merge-check", "off"])
        self.assertEqual(rc, 0, out)
        self.assertIn("merge check is now off (merge_check: off).", out)
        self.assertIn(OFF_WARNING, out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--merge-check", "auto"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("merge check is now off", out)     # back on: nothing to warn of

    def test_set_on_a_loop_that_never_posted_says_nothing(self):
        rc, out = self.run_cli(["set", "--loop", "widgets", "--merge-check", "off"])
        self.assertEqual(rc, 0, out)                       # a PAT reviewer: auto was off already
        self.assertNotIn("merge check is now off", out)

    def test_an_explicit_off_is_named_as_off_not_auto(self):
        # #1986: a move off the App with `merge_check: off` in the form.
        self.reviewer_on(BOT_REV)
        rc, out = self.apply_form("--identity-from-form", reviewer_login=tfs.REV,
                                  merge_check="off")
        self.assertEqual(rc, 0, out)
        self.assertIn("merge check is now off (merge_check: off)", out)
        self.assertNotIn("auto follows an App reviewer", out)
        self.assertIn("pinned to the App 5416187", out)     # the pin note, not the bare warning
        self.assertNotIn(OFF_WARNING, out)

    def test_auto_off_after_a_move_to_a_pat_names_auto(self):
        self.reviewer_on(BOT_REV)
        rc, out = self.apply_form("--identity-from-form", reviewer_login=tfs.REV)
        self.assertEqual(rc, 0, out)
        self.assertIn("merge check is now off (merge_check: auto, and the reviewer is no GitHub "
                      "App)", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
