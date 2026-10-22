"""doctor, selftest and backup on a GitHub App loop (#1163 M1c-2; #1181, #1182).

A loop built by ``init --app`` (an App reviewer and an App reader) reads as healthy where it is:
each credential check names the App and its key file (exists, private; never opened) and the
host's JWT signer, instead of "no token file" with a ``--token`` fix ``check_apps`` would refuse.
Expiry and scopes say an App has neither. selftest resolves each App identity to its bot without
``GET /user``. A backup records the App keys, and a restore names a missing one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import backup, config, doctor, gh, github_app, selftest  # noqa: E402

BOT_REV, BOT_READ = app_cli.BOT_REV, app_cli.BOT_READ
BAD = (doctor.ABSENT, doctor.MISMATCH)


class AppLoop(app_cli.Base):
    def setUp(self):
        super().setUp()
        self.rev_key, self.read_key = self.key("rev"), self.key("read")
        rc, out = self.run_cli(self.app_argv(
            reviewer=BOT_REV, reader=BOT_READ,
            apps=[(BOT_READ, 1, self.read_key), (BOT_REV, 2, self.rev_key)],
            tokens=[(tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)
        self.loop = config.load_id("widgets")
        signer = mock.patch.object(github_app, "signer", return_value=("openssl", Path("/x")))
        signer.start()
        self.addCleanup(signer.stop)


class Doctor(AppLoop):
    def test_an_app_seat_reads_as_verified_with_its_key(self):
        got = doctor.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        self.assertIn(f"{BOT_REV} → GitHub App 2, key {self.rev_key}", got.detail)
        self.assertNotIn("--token", got.fix or "")
        self.assertEqual(doctor.check_credential(self.loop, "fixer").status, doctor.VERIFIED)

    def test_an_app_reader_and_every_credential_line_pass(self):
        self.assertEqual(doctor.check_read_token(self.loop).status, doctor.VERIFIED)
        self.assertIn("a GitHub App", doctor.check_read_token(self.loop).detail)
        checks = doctor.check_tokens(self.loop)
        self.assertEqual({c.name for c in checks},
                         {f"token:{tfs.FIX}", f"app:{BOT_REV}", f"app:{BOT_READ}"})
        self.assertFalse([c for c in checks if c.status in BAD], [c.detail for c in checks])

    def test_a_loose_or_missing_key_is_named_with_a_key_fix(self):
        self.rev_key.chmod(0o644)
        got = doctor.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn(f"chmod 600 {self.rev_key}", got.fix)
        self.rev_key.unlink()
        got = doctor.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.ABSENT)
        self.assertIn("private key", got.fix)

    def test_no_signer_is_named(self):
        with mock.patch.object(github_app, "signer", return_value=("", None)):
            got = doctor.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.ABSENT)
        self.assertIn("cryptography", got.fix)

    def test_expiry_and_scopes_say_an_app_has_neither(self):
        with mock.patch.object(gh, "request", side_effect=AssertionError("no /user for an App")):
            checks = {c.name: c for c in doctor.check_token_expiry_scopes(self.loop, offline=True)}
        for login in (BOT_REV, BOT_READ):
            self.assertEqual(checks[f"token-expiry:{login}"].status, doctor.VERIFIED)
            self.assertIn("no PAT expiry", checks[f"token-expiry:{login}"].detail)

    def test_an_app_triage_login_and_adjudicator_read_as_verified(self):
        self.loop["triage"] = {**self.loop.get("triage", {}), "route": "widgets-triage",
                               "authors": ["fix-acct"], "labels": ["bug"]}
        triage = {c.name: c for c in doctor.check_triage(self.loop)}["credential:triage"]
        self.assertEqual(triage.status, doctor.VERIFIED, triage.detail)
        arbiter = self.key("adj")
        self.loop["apps"]["acme-arbiter[bot]"] = {"app_id": 3, "key_file": str(arbiter)}
        self.loop["seats"]["adjudicator"] = {"login": "acme-arbiter[bot]"}
        got = doctor.check_adjudicator_identity(self.loop)
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        self.loop["apps"]["acme-arbiter[bot]"]["key_file"] = str(self.rev_key)
        got = doctor.check_adjudicator_identity(self.loop)
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn("same token file as the reviewer", got.detail)


class Selftest(AppLoop):
    def identities(self, principal):
        report = selftest.Report(io.StringIO())

        def fake_principal(loop, login, fetch=None):
            if login == tfs.FIX:
                return {"login": tfs.FIX, "id": 3}
            return principal(login)
        with mock.patch.object(gh, "principal", side_effect=fake_principal), \
                mock.patch.object(gh, "fetch", side_effect=lambda loop, path, login=None:
                                  ({"full_name": "acme/widgets"}, "") if path.startswith("/repos/")
                                  else ({"login": login, "id": 3}, "")):
            ok = selftest.check_identities(report, self.loop)
        return ok, {r[1]: r for r in report.results}

    def test_app_identities_resolve_to_their_bots_without_user(self):
        ids = {BOT_REV: 346013884, BOT_READ: 346012008}
        ok, rows = self.identities(lambda login: {"login": login, "id": ids[login],
                                                  "type": "Bot", "app_id": 9})
        self.assertTrue(ok, rows)
        self.assertEqual(rows["github:reviewer"][2], selftest.PASS)
        self.assertIn("GitHub App 9", rows["github:reviewer"][3])

    def test_an_app_key_for_another_bot_or_no_installation_fails(self):
        ok, rows = self.identities(lambda login: {"login": "other[bot]", "id": 5})
        self.assertFalse(ok)
        self.assertIn("belongs to other[bot]", rows["github:reviewer"][3])

        def not_installed(login):
            raise github_app.AppError("App is not installed on acme/widgets")
        ok, rows = self.identities(not_installed)
        self.assertFalse(ok)
        self.assertIn("not installed", rows["github:reviewer"][3])


class Backup(AppLoop):
    def test_a_backup_records_app_keys_and_a_restore_names_a_missing_one(self):
        paths = backup.token_paths(self.loop)
        self.assertEqual(paths[BOT_REV], str(self.rev_key))
        self.assertEqual(paths[BOT_READ], str(self.read_key))
        manifest = {"tokens": {"widgets": paths}}
        self.assertEqual(backup.missing_tokens(manifest), [])
        self.rev_key.unlink()
        missing = backup.missing_tokens(manifest)
        self.assertEqual(len(missing), 1)
        self.assertIn(f"credential file for {BOT_REV}", missing[0])


if __name__ == "__main__":
    unittest.main()
