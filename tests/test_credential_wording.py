"""doctor and selftest name the right credential problem (#2109, #2243, #2454, #2458).

A signer that timed out, or a 200 ``/app`` answer that is some other JSON object, says nothing
against the App's key, so neither blames it. A shared credential is named as a credential file
(a PAT file or an App key), never a "token file" with a PAT-only fix. And an adjudicator login
that is not an App's bot is never offered ``--app <login>=...``, which ``set --app`` refuses.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_doctor as app_doctor  # noqa: E402
from diaktoros import doctor, gh, github_app  # noqa: E402
from diaktoros.doctor import identity_checks  # noqa: E402

BOT_REV, BOT_READ = app_doctor.BOT_REV, app_doctor.BOT_READ


class AppLive(app_doctor.AppLoop):
    """``check_app_live`` with ``_as_app`` answering ``app`` for ``/app`` (200) and an installation
    for the rest, or raising ``raises``."""

    def live(self, app=None, raises=None):
        def as_app(loop, login, path, method="GET", body=None):
            if raises:
                raise raises
            if path == "/app":
                return gh.Response(app, "", 200, {})
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

    def test_a_signing_failure_is_still_a_key_mismatch(self):
        got = self.live(raises=github_app.AppError("App JWT signing failed (openssl exit 1)"))
        self.assertEqual(got.status, doctor.MISMATCH, got.detail)
        self.assertIn("private key", got.fix)


if __name__ == "__main__":
    unittest.main()
