"""Doctor resolves a seat's profile home through ``config.profile_dir`` (#1485).

Doctor kept its own copy of the lookup, which did not strip the name: a profile written with
surrounding spaces resolved to ``profiles/' fixer '`` in doctor and to ``profiles/fixer``
everywhere else, so doctor reported a profile the runs actually use as missing.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from diaktoros import config, doctor  # noqa: E402
from diaktoros.doctor import identity_checks, seat_checks  # noqa: E402


class PaddedProfileName(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "profiles" / "fixer").mkdir(parents=True)

    def test_doctor_has_no_profile_dir_of_its_own(self):
        self.assertFalse(hasattr(doctor, "profile_dir"))

    def test_a_padded_profile_name_resolves_like_everywhere_else(self):
        loop = {"seats": {"fixer": {"profile": "  fixer \t"}}}
        check = seat_checks.check_profile(loop, "fixer")
        self.assertEqual(check.status, doctor.VERIFIED, check)
        self.assertIn(str(config.profile_dir("fixer")), check.detail)

    def test_a_padded_default_is_the_home_itself(self):
        loop = {"seats": {"reviewer": {"profile": " default "}}}
        check = seat_checks.check_profile(loop, "reviewer")
        self.assertEqual(check.status, doctor.VERIFIED, check)
        self.assertIn(str(config.home()), check.detail)

    def test_the_credential_check_reads_the_padded_profiles_env(self):
        (self.home / "profiles" / "fixer" / ".env").write_text("GH_TOKEN=ghp_x\n")
        loop = {"seats": {"fixer": {"profile": " fixer ", "login": ""}}}
        check = identity_checks.check_credential(loop, "fixer")
        self.assertIn(f"GH_TOKEN in {self.home / 'profiles' / 'fixer' / '.env'}", check.detail)


if __name__ == "__main__":
    unittest.main()
