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
        self.assertEqual(tokens[ASKED], str(config._path("/new.pat")))
        self.assertEqual(apps, {})

    def test_status_lines_list_one_login_once(self):
        loop = {"reviewer_seat": MAPPED, "read_token": ASKED, "tokens": {MAPPED: "/x/a.pat"},
                "seats": {"reviewer": {"login": MAPPED}, "fixer": {"login": "other"}}}
        lines = cli_common._credential_lines(loop)
        self.assertFalse(any(line.startswith("read ") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
