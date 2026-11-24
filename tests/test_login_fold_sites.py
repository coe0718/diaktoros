"""#1940: the sites #1924 moved to ``util.login_key`` without a test, and the last credential-map
lookup (``ingress_app`` in ``apps``) moved to ``util.login_entry``.

Each test spells one login two ways (``ß``/``ss``: one login to ``casefold``, two to ``lower``)
beside an unrelated mapping that must survive, so a ``lower()`` revert at a site turns it red.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import config, gh, util  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.cli import setup_command  # noqa: E402

MAPPED, ASKED = "fix-\u00df", "fix-ss"                    # one login to casefold, two to lower
BOT_MAPPED, BOT_ASKED = "rd-\u00df[bot]", "rd-ss[bot]"


class SetupFolds(app_cli.Base):
    """``setup_command._app_logins_test`` and the hook-admin check against the three roles."""

    def setup_args(self, *flags):
        return app_cli.Setup.argv(self, *flags)

    def asked_questions(self, args):
        asked = []
        orig = setup_command._ask
        setup_command._ask = lambda question, default="", interactive=False: \
            asked.append(question) or default
        self.addCleanup(setattr, setup_command, "_ask", orig)
        setup_command._setup_init_argv(args, "acme/widgets", "widgets", interactive=False)
        return asked

    def test_the_app_login_test_folds_the_app_login(self):
        has_app = setup_command._app_logins_test(
            argparse.Namespace(app=[f"{MAPPED}=1:/k/x.pem", "keep[bot]=2:/k/y.pem"]))
        self.assertTrue(has_app(ASKED))
        self.assertTrue(has_app("keep[bot]"))
        self.assertFalse(has_app("unrelated"))

    def test_the_app_login_test_folds_the_queried_login(self):
        # The reverse order: the App is given ASCII, the queried login is the ``ß`` spelling, so
        # the query side's fold is what matches (``lower`` would keep ``ß`` and miss it).
        has_app = setup_command._app_logins_test(
            argparse.Namespace(app=[f"{ASKED}=1:/k/x.pem", "keep[bot]=2:/k/y.pem"]))
        self.assertTrue(has_app(MAPPED))
        self.assertTrue(has_app("keep[bot]"))
        self.assertFalse(has_app("unrelated"))

    def test_setup_asks_no_file_for_an_app_login_spelled_the_other_way(self):
        key = self.key("fix")
        args = self.setup_args("--reviewer", tfs.REV, "--fixer", ASKED,
                               "--reviewer-profile", "critic", "--fixer-profile", "coder",
                               "--read-token", tfs.READER,
                               "--read-token-file", str(self.pats["read"]),
                               "--reviewer-token", str(self.pats["rev"]),
                               "--host", "https://gateway.example", "--app", f"{MAPPED}=5:{key}")
        asked = self.asked_questions(args)
        self.assertFalse(any(q.startswith(f"token file for {ASKED}") for q in asked), asked)

    def test_setup_asks_no_file_for_an_admin_that_is_a_role_spelled_the_other_way(self):
        # The hook admin is the reviewer, the fixer or the reader spelled the other way: it has
        # that role's file, so none is asked for it. An unrelated admin is still asked.
        for role in ("reviewer", "fixer", "reader"):
            logins = {"reviewer": tfs.REV, "fixer": tfs.FIX, "reader": tfs.READER, role: MAPPED}
            for admin, asked_for in ((ASKED, False), ("hook-owner", True)):
                with self.subTest(role=role, admin=admin):
                    args = self.setup_args(
                        "--reviewer", logins["reviewer"], "--fixer", logins["fixer"],
                        "--reviewer-profile", "critic", "--fixer-profile", "coder",
                        "--read-token", logins["reader"],
                        "--read-token-file", str(self.pats["read"]),
                        "--reviewer-token", str(self.pats["rev"]),
                        "--fixer-token", str(self.pats["fix"]),
                        "--host", "https://gateway.example", "--admin-token", admin)
                    asked = self.asked_questions(args)
                    self.assertEqual(any(q == f"token file for {admin}" for q in asked),
                                     asked_for, asked)


class InitFormCredentials(app_cli.Base):
    """``install._form_credentials``: a form file never overrides ``--token`` spelled the other way."""

    def test_the_helper_skips_a_role_given_a_token_spelled_the_other_way(self):
        from diaktoros.cli import install
        form = {"adjudicator_login": "", "reviewer_token_file": "/f/rev.pat",
                "fixer_token_file": "/f/fix.pat", "adjudicator_token_file": "",
                **{f"{role}_{suffix}": "" for role in ("reviewer", "fixer", "adjudicator")
                   for suffix in ("app_id", "app_key_file")}}
        got = install._form_credentials(form, {util.login_key(MAPPED)}, tfs.REV, ASKED, "")
        self.assertNotIn("fixer_token_file", got)
        self.assertEqual(got["reviewer_token_file"], "/f/rev.pat")   # the unrelated role keeps it

    def test_the_helper_folds_the_seat_login(self):
        # The reverse order: the seat is the ``ß`` spelling, the explicit set holds the folded
        # ASCII one, so the seat side's fold is what matches.
        from diaktoros.cli import install
        form = {"adjudicator_login": "", "reviewer_token_file": "/f/rev.pat",
                "fixer_token_file": "/f/fix.pat", "adjudicator_token_file": "",
                **{f"{role}_{suffix}": "" for role in ("reviewer", "fixer", "adjudicator")
                   for suffix in ("app_id", "app_key_file")}}
        got = install._form_credentials(form, {util.login_key(ASKED)}, tfs.REV, MAPPED, "")
        self.assertFalse([k for k in got if k.startswith("fixer_")], got)
        self.assertEqual(got["reviewer_token_file"], "/f/rev.pat")   # the unrelated role keeps it

    def test_init_keeps_the_cli_app_over_the_forms_file(self):
        # Through `init`, the --app case: `--app rd-ss[bot]=…` (an App login is ASCII) with
        # `--fixer rd-ß[bot]` and a form fixer App. The CLI App stays the fixer's credential; the
        # form's App does not replace it. (A form *token file* would not show the fold here:
        # `select_credentials` ignores a PAT field for a login that already has an App.)
        key = self.key("fix")
        argv = self.app_argv(reviewer=tfs.REV,
                             tokens=[(tfs.READER, self.pats["read"]), (tfs.REV, self.pats["rev"])],
                             apps=[(BOT_ASKED, 9, key)])
        argv[argv.index(tfs.FIX)] = BOT_MAPPED
        rc, out = self.run_cli(argv, settings={"fixer_app_id": "77",
                                               "fixer_app_key_file": str(self.key("form-fix"))})
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(loop["apps"], {BOT_ASKED: {"app_id": 9, "key_file": str(key)}})
        self.assertEqual(sorted(loop["tokens"]), sorted([tfs.READER, tfs.REV]), loop["tokens"])

    def test_init_keeps_the_cli_token_over_the_forms_file(self):
        # Through `init` itself: `--token fix-ß=…` with `--fixer fix-ss` and a form fixer file.
        argv = self.app_argv(reviewer=tfs.REV, tokens=[(tfs.READER, self.pats["read"]),
                                                       (MAPPED, self.pats["fix"]),
                                                       (tfs.REV, self.pats["rev"])])
        argv[argv.index(tfs.FIX)] = ASKED
        rc, out = self.run_cli(argv, settings={"fixer_token_file": str(self.pat("form-fix"))})
        self.assertEqual(rc, 0, out)
        tokens = self.loop_json()["tokens"]
        self.assertEqual(tokens.get(MAPPED), str(self.pats["fix"]), tokens)
        self.assertEqual(tokens.get(tfs.REV), str(self.pats["rev"]), tokens)
        self.assertEqual(len(tokens), 3, tokens)


class HookWriteNeed(unittest.TestCase):
    """``cli_common.hook_write_need``'s two reader comparisons."""

    def test_the_pat_wording_names_the_reader_spelled_the_other_way(self):
        loop = {"repo": "o/r", "read_token": MAPPED, "tokens": {MAPPED: "/x/r.pat",
                                                                "other": "/x/o.pat"}}
        self.assertIn("that is the reader's file", cli_common.hook_write_need(loop, ASKED))
        self.assertIn("check that login's token file", cli_common.hook_write_need(loop, "other"))

    def test_the_app_wording_names_the_reader_spelled_the_other_way(self):
        loop = {"repo": "o/r", "read_token": BOT_MAPPED, "tokens": {},
                "apps": {BOT_ASKED: {"app_id": 1}, "keep[bot]": {"app_id": 2}}}
        self.assertIn("that is the reader's App", cli_common.hook_write_need(loop, BOT_ASKED))
        self.assertIn("check that App's permission", cli_common.hook_write_need(loop, "keep[bot]"))


class ReplacementBranches(app_cli.Base):
    """``set --app``, ``triage --token`` and ``identity``: the entry under the other spelling goes,
    an unrelated one stays."""

    def test_set_app_replaces_the_mapping_under_the_other_spelling(self):
        from diaktoros.cli import set_command
        args = argparse.Namespace(app=[f"{BOT_ASKED}=7:{self.key('rd')}"])
        tokens = {BOT_MAPPED: "/x/old.pat", "keep": "/x/keep.pat"}
        apps = {BOT_MAPPED: {"app_id": 1, "key_file": "/k/old.pem"},
                "keep[bot]": {"app_id": 2, "key_file": "/k/keep.pem"}}
        set_command._map_app_pairs(args, {}, tokens, apps, ("", BOT_ASKED))
        self.assertEqual(tokens, {"keep": "/x/keep.pat"})
        self.assertEqual(sorted(apps), sorted([BOT_ASKED, "keep[bot]"]))
        self.assertEqual(apps[BOT_ASKED]["app_id"], 7)

    def test_triage_token_replaces_the_mapping_under_the_other_spelling(self):
        from diaktoros.cli import triage_command
        loop = {"read_token": tfs.READER, "seats": {"reviewer": {"login": tfs.REV},
                                                  "fixer": {"login": tfs.FIX}},
                "tokens": {"hk-\u00df": "/x/old.pat", "keep": "/x/keep.pat"}}
        new = self.pat("hk")
        got = triage_command._mapped_tokens(argparse.Namespace(token=[f"hk-ss={new}"]), loop)
        self.assertEqual(got, {"keep": "/x/keep.pat", "hk-ss": str(new)})

    def test_identity_pat_replaces_the_app_under_the_other_spelling(self):
        # The reader is an App; `identity --pat` moves it to a PAT spelled the other way. The App
        # entry goes (one login, one credential); the reviewer's App stays.
        rc, out = self.run_cli(self.app_argv(
            reviewer=app_cli.BOT_REV, reader=BOT_ASKED, tokens=[(tfs.FIX, self.pats["fix"])],
            apps=[(BOT_ASKED, 1, self.key("rd")), (app_cli.BOT_REV, 2, self.key("rev"))]))
        self.assertEqual(rc, 0, out)
        resolved = lambda loop, login, fetch=None: {"login": login, "id": 1}  # noqa: E731
        with mock.patch.object(gh, "principal", side_effect=resolved):
            rc, out = self.run_cli(["identity", "--loop", "widgets", "--seat", "read",
                                    "--pat", f"{BOT_MAPPED}={self.pat('rd-pat')}"])
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(sorted(loop["apps"]), [app_cli.BOT_REV])
        self.assertIn(BOT_MAPPED, loop["tokens"])


class IngressAppLookup(unittest.TestCase):
    """#1940 (a): ``ingress_app``'s membership in ``apps`` is made by ``util.login_entry``, like
    every credential-map lookup, at the loader's check (``loop_checks``, called by normalize)."""

    def loop(self, **extra):
        return {"ingress": "app-webhook", "apps": {"acme-reader[bot]": {"app_id": 1}},
                "fixer_check": "", "required_checks": [], "human_paths": [], "generated": [],
                "ci_coverage": {}, "prices": {}, "merge_check": "auto", **extra}

    def check(self, loop):
        from diaktoros.config import loop_checks
        loop_checks._check_lists_and_modes({**config.DEFAULTS, **loop}, "t")

    def test_the_lookup_is_login_entry(self):
        with mock.patch.object(util, "login_entry", wraps=util.login_entry) as entry:
            self.check(self.loop(ingress_app="Acme-Reader[bot]"))
        self.assertEqual([c.args[1] for c in entry.call_args_list], ["Acme-Reader[bot]"])

    def test_a_mapped_app_in_any_case_passes_and_an_unmapped_one_is_refused(self):
        self.check(self.loop(ingress_app="ACME-reader[bot]"))
        for bad in ("other[bot]", "acme-reader", ""):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "ingress_app"):
                self.check(self.loop(ingress_app=bad))


if __name__ == "__main__":
    unittest.main()
