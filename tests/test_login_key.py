"""#1210: one folding rule for "which mapped login is this", shared by every layer.

``ß`` folds to ``ss`` under ``casefold`` but not under ``lower``: the lookups (``login_entry``,
the desktop page) used ``casefold`` while the form's credential selection and the CLI's status
lines used ``lower``, so one mapped login was two logins depending on the layer asked.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import config, desktop_api, util  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

MAPPED, ASKED = "fix-\u00df", "fix-ss"          # one login to casefold, two to lower


class LoginKey(unittest.TestCase):
    def test_the_layers_agree_on_which_mapped_login_this_is(self):
        loop = {"tokens": {MAPPED: "/x/fix.pat"}, "seats": {"fixer": {"login": ASKED}},
                "reviewer_seat": "rev-bot"}
        self.assertEqual(util.login_entry(loop["tokens"], ASKED), "/x/fix.pat")
        self.assertEqual(desktop_api._seat_credential(loop, ASKED)["token_file"], "/x/fix.pat")
        self.assertEqual(cli_common._token_ref(loop, ASKED), "/x/fix.pat")
        self.assertEqual(cli_common.admin_unmapped(loop, ASKED), "")
        self.assertEqual(cli_common.admin_unmapped({"tokens": {}}, MAPPED, also={ASKED}), "")

    def test_the_form_replaces_the_mapped_login_instead_of_adding_a_second(self):
        loop = {"tokens": {MAPPED: "/old.pat"}, "seats": {"fixer": {"login": ASKED}}}
        tokens, apps, _selected, _ignored = settings_form.select_credentials(
            loop, {"fixer_token_file": "/new.pat"})
        self.assertEqual(list(tokens), [ASKED])
        self.assertEqual(tokens[ASKED], str(config.expand_path("/new.pat")))
        self.assertEqual(apps, {})

    def test_status_lines_list_one_login_once(self):
        loop = {"reviewer_seat": MAPPED, "read_token": ASKED, "tokens": {MAPPED: "/x/a.pat"},
                "seats": {"reviewer": {"login": MAPPED}, "fixer": {"login": "other"}}}
        lines = cli_common._credential_lines(loop)
        self.assertFalse(any(line.startswith("read ") for line in lines), lines)


class StatusLinesSeats(unittest.TestCase):
    """F2: the two seats, and a read token spelled the other way round."""

    def test_two_spellings_of_one_seat_login_list_one_seat(self):
        loop = {"read_token": "reader", "tokens": {MAPPED: "/x/a.pat", "reader": "/x/r.pat"},
                "seats": {"reviewer": {"login": MAPPED}, "fixer": {"login": ASKED}}}
        lines = cli_common._credential_lines(loop)
        self.assertTrue(any(line.startswith("reviewer ") for line in lines), lines)
        self.assertFalse(any(line.startswith("fixer ") for line in lines), lines)

    def test_the_reverse_spelling_order_lists_one_seat_too(self):
        # The comparison alone: `seen` holds the reviewer's key, the fixer's login is spelled the
        # other way. (With the reviewer spelled `fix-ß`, a `lower()` comparison still matched,
        # because the key stored in `seen` was already casefolded.)
        loop = {"read_token": "reader", "tokens": {ASKED: "/x/a.pat", "reader": "/x/r.pat"},
                "seats": {"reviewer": {"login": ASKED}, "fixer": {"login": MAPPED}}}
        lines = cli_common._credential_lines(loop)
        self.assertTrue(any(line.startswith("reviewer ") for line in lines), lines)
        self.assertFalse(any(line.startswith("fixer ") for line in lines), lines)

    def test_a_distinct_fixer_is_kept(self):
        loop = {"read_token": "reader", "tokens": {MAPPED: "/x/a.pat", "other": "/x/o.pat"},
                "seats": {"reviewer": {"login": MAPPED}, "fixer": {"login": "other"}}}
        lines = cli_common._credential_lines(loop)
        self.assertTrue(any(line.startswith("fixer other") for line in lines), lines)

    def test_the_read_token_is_compared_by_the_same_key(self):
        loop = {"read_token": MAPPED, "tokens": {ASKED: "/x/a.pat"},
                "seats": {"reviewer": {"login": ASKED}, "fixer": {"login": "other"}}}
        lines = cli_common._credential_lines(loop)
        self.assertFalse(any(line.startswith("read ") for line in lines), lines)


class SelectCredentialsMaps(unittest.TestCase):
    """F1: each fold in ``select_credentials``, with an unrelated mapping that must survive."""
    APP = {"app_id": 5, "key_file": "/k/old.pem"}

    def select(self, loop, settings):
        loop = {"seats": {"fixer": {"login": ASKED}}, **loop}
        return settings_form.select_credentials(loop, settings)

    def test_an_inherited_pat_is_recognised_across_spellings(self):
        # existing_pat: the form repeats the mapped PAT's path beside an App pair, not a conflict.
        tokens, apps, _s, ignored = self.select(
            {"tokens": {MAPPED: "/x/fix.pat", "keep": "/x/keep.pat"}},
            {"fixer_token_file": "/x/fix.pat", "fixer_app_id": "7", "fixer_app_key_file": "/k/new.pem"})
        self.assertEqual(ignored, {"fixer_token_file": ASKED})
        self.assertEqual(list(apps), [ASKED])
        self.assertEqual(list(tokens), ["keep"])

    def test_an_existing_app_is_recognised_across_spellings(self):
        # existing_app: a PAT field aimed at an App-mapped login is stale, never a second PAT.
        tokens, apps, selected, ignored = self.select(
            {"tokens": {"keep": "/x/keep.pat"}, "apps": {MAPPED: dict(self.APP)}},
            {"fixer_token_file": "/x/new.pat"})
        self.assertEqual(list(tokens), ["keep"])
        self.assertEqual(apps, {MAPPED: self.APP})
        self.assertEqual((selected, ignored), ({}, {"fixer_token_file": ASKED}))

    def test_a_new_app_removes_the_pat_under_the_other_spelling(self):
        tokens, apps, _s, _i = self.select(
            {"tokens": {MAPPED: "/x/old.pat", "keep": "/x/keep.pat"}},
            {"fixer_app_id": "7", "fixer_app_key_file": "/k/new.pem"})
        self.assertEqual(list(tokens), ["keep"])
        self.assertEqual(list(apps), [ASKED])

    def test_a_new_app_replaces_the_app_under_the_other_spelling(self):
        tokens, apps, _s, _i = self.select(
            {"tokens": {"keep": "/x/keep.pat"},
             "apps": {MAPPED: dict(self.APP), "keep[bot]": dict(self.APP)}},
            {"fixer_app_id": "7", "fixer_app_key_file": "/k/new.pem"})
        self.assertEqual(sorted(apps), sorted([ASKED, "keep[bot]"]))
        self.assertEqual(apps[ASKED]["app_id"], 7)
        self.assertEqual(list(tokens), ["keep"])


class AppPredicates(unittest.TestCase):
    """F3: ``is_app``, ``reviewer_is_app`` and the reviewer App's id fold as ``login_entry`` does."""
    BOT_MAPPED, BOT_ASKED = "fix-\u00df[bot]", "fix-ss[bot]"

    def test_is_app_and_reviewer_is_app_agree_with_login_entry(self):
        loop = {"reviewer_seat": self.BOT_ASKED, "apps": {self.BOT_MAPPED: {"app_id": 9}}}
        self.assertIsNotNone(util.login_entry(loop["apps"], self.BOT_ASKED))
        self.assertTrue(config.is_app(loop, self.BOT_ASKED))
        self.assertTrue(config.reviewer_is_app(loop))
        self.assertFalse(config.is_app(loop, "other[bot]"))

    def test_a_pat_under_the_other_spelling_still_wins(self):
        loop = {"reviewer_seat": self.BOT_ASKED, "apps": {self.BOT_MAPPED: {"app_id": 9}},
                "tokens": {self.BOT_ASKED: "/x/p.pat"}}
        self.assertFalse(config.reviewer_is_app(loop))

    def test_the_reviewer_apps_id_is_found_across_spellings(self):
        from diaktoros import protection
        loop = {"reviewer_seat": self.BOT_ASKED, "apps": {self.BOT_MAPPED: {"app_id": 9}}}
        self.assertEqual(protection.reviewer_app_id(loop), 9)

    def test_seat_or_reader_login_folds_the_same_way(self):
        from diaktoros.cli import triage_command
        loop = {"read_token": MAPPED, "seats": {"reviewer": {"login": "a"}, "fixer": {"login": "b"}}}
        self.assertTrue(triage_command._is_seat_or_reader_login(loop, ASKED))
        self.assertFalse(triage_command._is_seat_or_reader_login(loop, "unrelated"))


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import gh  # noqa: E402
from unittest import mock  # noqa: E402

RD_MAPPED, RD_ASKED = "rd-\u00df", "rd-ss"


class CliReplacesTheMappedLogin(app_cli.Base):
    """F3: ``set --token`` and ``identity --pat`` replace the entry under the other spelling."""

    def install(self):
        argv = self.app_argv(reader=RD_MAPPED, reviewer=tfs.REV,
                             tokens=[(RD_MAPPED, self.pats["read"]), (tfs.FIX, self.pats["fix"]),
                                     (tfs.REV, self.pats["rev"])])
        self.assertEqual(self.run_cli(argv)[0], 0)

    def test_set_token_replaces_the_reader_under_the_other_spelling(self):
        self.install()
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", RD_ASKED,
                                "--token", f"{RD_ASKED}={self.pat('rd2')}"])
        self.assertEqual(rc, 0, out)
        tokens = self.loop_json()["tokens"]
        self.assertEqual(sorted(tokens), sorted([RD_ASKED, tfs.FIX, tfs.REV]))

    def test_identity_pat_replaces_the_reader_under_the_other_spelling(self):
        self.install()
        resolved = lambda loop, login, fetch=None: {"login": login, "id": 1}  # noqa: E731
        with mock.patch.object(gh, "principal", side_effect=resolved):
            rc, out = self.run_cli(["identity", "--loop", "widgets", "--seat", "read",
                                    "--pat", f"{RD_ASKED}={self.pat('rd3')}"])
        self.assertEqual(rc, 0, out)
        tokens = self.loop_json()["tokens"]
        self.assertEqual(sorted(tokens), sorted([RD_ASKED, tfs.FIX, tfs.REV]))


if __name__ == "__main__":
    unittest.main()
