"""doctor and selftest name the right credential problem (#2109, #2243, #2454, #2458).

A signer that timed out, or a 200 ``/app`` answer that is some other JSON object, says nothing
against the App's key, so neither blames it. A shared credential is named as a credential file
(a PAT file or an App key), never a "token file" with a PAT-only fix. And an adjudicator login
that is not an App's bot is never offered ``--app <login>=...``, which ``set --app`` refuses.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_doctor as app_doctor  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import doctor, gh, github_app, selftest  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.config import credentials  # noqa: E402
from diaktoros.doctor import identity_checks  # noqa: E402

BOT_REV, BOT_READ = app_doctor.BOT_REV, app_doctor.BOT_READ
PERSON = "ghost-account"


class AppLive(app_doctor.AppLoop):
    """``check_app_live`` with ``_as_app`` answering ``app`` for ``/app`` (200) and an installation
    for the rest, or raising ``raises``."""

    def live(self, app=None, raises=None, install_raises=None):
        def as_app(loop, login, path, method="GET", body=None):
            if raises:
                raise raises
            if path == "/app":
                return gh.Response(app, "", 200, {})
            if install_raises:
                raise install_raises
            return gh.Response({"id": 1}, "", 200, {})
        with mock.patch.object(github_app, "_as_app", side_effect=as_app):
            return identity_checks.check_app_live(self.loop, BOT_REV, offline=False)

    def test_a_signing_timeout_is_unknown_and_does_not_blame_the_key(self):
        # #2109: SignTimeout is an AppError, but a signer that hung never judged the key.
        got = self.live(raises=github_app.SignTimeout(
            "App JWT signing failed (cryptography TimeoutExpired)"))
        self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
        self.assertIn("TimeoutExpired", got.detail)
        self.assertIn("did not finish in time", got.detail)
        self.assertNotIn("private key", got.fix)
        self.assertIn("the key file is not implicated", got.fix)

    def test_a_timeout_after_a_mismatched_app_still_reports_the_mismatch(self):
        # #2560 review F1: each read signs its own JWT; what /app said stands.
        timeout = github_app.SignTimeout("App JWT signing failed (cryptography TimeoutExpired)")
        got = self.live({"id": 99, "slug": "other"}, install_raises=timeout)
        self.assertEqual(got.status, doctor.MISMATCH, got.detail)
        self.assertIn("signs as App 99 (other)", got.detail)
        got = self.live({"id": 2, "slug": "other"}, install_raises=timeout)
        self.assertEqual(got.status, doctor.MISMATCH, got.detail)
        self.assertIn("is other[bot]", got.detail)

    def test_a_timeout_after_a_matching_app_names_only_the_installation_unknown(self):
        got = self.live({"id": 2, "slug": "acme-reviewer"}, install_raises=github_app.SignTimeout(
            "App JWT signing failed (cryptography TimeoutExpired)"))
        self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
        self.assertIn("GET /app signed with the key and matched", got.detail)
        self.assertIn("the installation was not read", got.detail)
        self.assertNotIn("never judged", got.detail)
        self.assertIn("the key file is not implicated", got.fix)

    def test_a_signing_failure_is_still_a_key_mismatch(self):
        got = self.live(raises=github_app.AppError("App JWT signing failed (openssl exit 1)"))
        self.assertEqual(got.status, doctor.MISMATCH, got.detail)
        self.assertIn("private key", got.fix)

    def test_a_200_json_object_without_app_fields_does_not_blame_the_key(self):
        # #2458: a proxy's own JSON object is not an App that the key signs as.
        for body in ({"message": "portal authentication needed"}, {"id": 2},
                     {"slug": "acme-reviewer"}, {"id": "2", "slug": "acme-reviewer"}):
            with self.subTest(body=body):
                got = self.live(body)
                self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
                self.assertIn("not the expected JSON", got.detail)
                self.assertNotIn("signs as App", got.detail)
                self.assertNotIn("apps", got.fix)
                self.assertIn("the key file is not implicated", got.fix)

    def test_a_well_formed_other_app_is_still_a_mismatch(self):
        got = self.live({"id": 99, "slug": "other"})
        self.assertEqual(got.status, doctor.MISMATCH, got.detail)
        self.assertIn("signs as App 99 (other)", got.detail)
        self.assertEqual(self.live({"id": 2, "slug": "acme-reviewer"}).status, doctor.VERIFIED)


class SharedCredential(app_doctor.AppLoop):
    """#2243: an App key shared by two identities is a shared credential file, not a token file."""

    def share_the_readers_key_with_the_reviewer(self):
        key = next(k for k in self.loop["apps"] if k.casefold() == BOT_REV.casefold())
        self.loop["apps"][key]["key_file"] = str(self.read_key)

    def test_selftest_names_a_shared_app_key_as_a_credential_file(self):
        self.share_the_readers_key_with_the_reviewer()
        ids = {BOT_REV.casefold(): 346013884, BOT_READ.casefold(): 346012008,
               tfs.FIX.casefold(): 3}
        report = selftest.Report(io.StringIO())
        with mock.patch.object(gh, "principal", side_effect=lambda loop, login, fetch=None: {
                    "login": login, "id": ids[login.casefold()], "app_id": 9}), \
                mock.patch.object(gh, "fetch", side_effect=lambda loop, path, login=None:
                                  ({"login": login, "id": 3}, "")):
            ok = selftest.check_identities(report, self.loop)
        rows = {r[1]: r for r in report.results}
        self.assertFalse(ok)
        _step, _name, status, detail, fix = rows["github:distinct-files"]
        self.assertEqual(status, selftest.FAIL)
        self.assertEqual(detail, "two identities share one credential file")
        self.assertEqual(fix, "give every identity its own PAT file or GitHub App key")
        self.assertNotIn("token file", detail)

    def test_the_reader_check_names_a_shared_app_key_as_a_credential_file(self):
        self.share_the_readers_key_with_the_reviewer()
        problem = credentials.reader_problem(self.loop)
        self.assertIn(f"the reader {BOT_READ!r} and the reviewer seat", problem)
        self.assertIn("read the same credential file", problem)
        self.assertNotIn("token file", problem)

    def test_a_credential_file_that_cannot_be_compared_is_named_kind_neutrally(self):
        self.read_key.unlink()
        self.assertEqual(credentials._shared_token_file(self.read_key, self.rev_key),
                         "cannot be told apart by credential file (No such file or directory)")


class AdjudicatorAppFix(app_doctor.AppLoop):
    """#2454: the App half of an unmapped adjudicator's fix is one `set --app` would take."""

    def unmapped(self, login):
        self.loop["seats"]["adjudicator"] = {"login": login}
        doctor_fix = identity_checks.check_adjudicator_identity(self.loop)
        self.assertEqual(doctor_fix.status, doctor.ABSENT, doctor_fix.detail)
        return doctor_fix.fix, selftest._unmapped_fix(self.loop, "adjudicator", login)

    def test_a_person_login_is_told_to_name_the_apps_bot_instead(self):
        for fix in self.unmapped(PERSON):
            with self.subTest(fix=fix):
                self.assertIn(f"--token {PERSON}=/path/to/pat", fix)
                self.assertNotIn(f"--app {PERSON}=", fix)
                self.assertIn("for a GitHub App adjudicator, name its bot login instead: "
                              "--adjudicator-login <app-slug>[bot] "
                              "--app <app-slug>[bot]=APP_ID:/abs/key.pem", fix)
                self.assertIn(f"{PERSON!r} is not an App's bot login", fix)
        # What the old advice named, `set --app` refuses.
        with mock.patch("builtins.print"):
            self.assertIsNone(cli_common._split_app_pair(f"{PERSON}=7:/abs/key.pem"))

    def test_a_person_login_spelled_like_the_placeholder_is_not_offered_app(self):
        # #2560 review F2: an adjudicator's login is real, even one spelled LOGIN.
        for fix in self.unmapped("LOGIN"):
            with self.subTest(fix=fix):
                self.assertNotIn("--app LOGIN=", fix)
                self.assertIn("--adjudicator-login <app-slug>[bot]", fix)
        with mock.patch("builtins.print"):
            self.assertIsNone(cli_common._split_app_pair("LOGIN=7:/abs/key.pem"))

    def test_a_bot_login_keeps_the_same_login_app_form(self):
        bot = "acme-arbiter[bot]"
        for fix in self.unmapped(bot):
            with self.subTest(fix=fix):
                self.assertIn(f"--app {bot}=APP_ID:/abs/key.pem for a GitHub App adjudicator", fix)
                self.assertNotIn("--adjudicator-login <app-slug>[bot]", fix)

    def test_the_reader_alternative_is_unchanged(self):
        self.assertEqual(credentials.reader_app_alternative(PERSON),
                         "for a GitHub App reader, name its bot login instead: --read-token "
                         "<app-slug>[bot] --app <app-slug>[bot]=APP_ID:/abs/key.pem — "
                         f"{PERSON!r} is not an App's bot login")
        self.assertEqual(credentials.reader_app_alternative(),
                         "--app <that login>=APP_ID:/abs/key.pem for a GitHub App reader")
        self.assertEqual(credentials.reader_app_alternative("", "LOGIN"),
                         "--app LOGIN=APP_ID:/abs/key.pem for a GitHub App reader")
        # #2450: a reader really named LOGIN is a person's login, not the stand-in.
        self.assertNotIn("--app LOGIN=", credentials.reader_app_alternative("LOGIN"))


if __name__ == "__main__":
    unittest.main()
