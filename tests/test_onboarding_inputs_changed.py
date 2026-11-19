"""Follow-ups to #2512's onboarding fingerprints: stale reasons and selftest's inputs:changed.

* #2514: a stale stage's reason names what its fingerprint now covers (credential contents for
  ``identities``, loop settings for ``loop``).

GitHub, the model and bubblewrap are mocked by ``test_selftest.SelftestBase``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_selftest import SelftestBase  # noqa: E402
from diaktoros import onboarding  # noqa: E402


def rotate(path: pathlib.Path, letter: str) -> None:
    path.write_text("ghp_" + letter * 36 + "\n")


class StaleReasons(SelftestBase):
    """#2514: the reason says which kind of input changed, including the new ones."""

    def record_clean_run(self):
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 0, text)

    def test_a_rotated_token_names_the_credential_file_contents(self):
        self.record_clean_run()
        rotate(pathlib.Path(self.fx.loop["tokens"]["reviewer"]), "Z")
        row = onboarding.inspect(self.fx.loop, runtime_file=self.fx.runtime_file)["identities"]
        self.assertEqual(row["status"], onboarding.STALE, row)
        self.assertIn("identities fingerprint changed", row["reason"])
        self.assertIn("the contents of a token or App key file", row["reason"])

    def test_a_changed_loop_setting_names_a_loop_setting(self):
        self.record_clean_run()
        edited = {**self.fx.loop, "host": "hooks.example.net"}
        rows = onboarding.inspect(edited, runtime_file=self.fx.runtime_file)
        self.assertEqual(rows["identities"]["status"], onboarding.PASSED, rows)
        self.assertEqual(rows["loop"]["status"], onboarding.STALE, rows)
        self.assertIn("loop fingerprint changed", rows["loop"]["reason"])
        self.assertIn("a loop setting such as host or base", rows["loop"]["reason"])


if __name__ == "__main__":
    unittest.main()
