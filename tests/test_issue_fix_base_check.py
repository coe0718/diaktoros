"""An issue fix's base-commit check (#214) stays small however far the base has moved on.

Seen live 2026-10-27: issue fixes queued at v0.5.0 (062ca6a) all failed staging with "GitHub
response exceeds bounds" once main was 50 commits ahead, because the check asked GitHub to
compare head...base, whose answer lists every file and commit in between.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

from diaktoros import trusted_fetch

LOOP = {"base": "main"}
HEAD = "a" * 40


class BaseCheck(unittest.TestCase):
    def check(self, status):
        calls = []

        def answer(loop, path, login, limit=trusted_fetch._MAX_METADATA_RESPONSE):
            calls.append((path, limit))
            return {"status": status, "files": [], "commits": []}
        with mock.patch.object(trusted_fetch, "_json", side_effect=answer):
            trusted_fetch._base_head(LOOP, "o/r", HEAD, "reader")
        return calls

    def test_it_asks_base_first_so_the_answer_lists_nothing_in_between(self):
        self.assertEqual(self.check("behind"),
                         [(f"/repos/o/r/compare/main...{HEAD}",
                           trusted_fetch._MAX_METADATA_RESPONSE)])

    def test_a_head_on_the_base_passes_whether_or_not_the_base_moved(self):
        for status in ("behind", "identical"):
            with self.subTest(status=status):
                self.check(status)

    def test_a_head_off_the_base_is_refused(self):
        for status in ("ahead", "diverged", None):
            with self.subTest(status=status), self.assertRaises(trusted_fetch.FetchDenied):
                self.check(status)

    def test_a_base_far_ahead_still_fits_the_bound(self):
        # The live answer's shape for main...062ca6a once main was 50 commits ahead: no files,
        # no commits. The bounded reader takes it; the old direction's 755 KB did not fit.
        body = b'{"status":"behind","ahead_by":0,"behind_by":50,"files":[],"commits":[]}'
        response = mock.MagicMock(status=200, headers={"Content-Length": str(len(body))})
        response.read.side_effect = [body, b""]
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response), \
                mock.patch("diaktoros.gh.token", return_value="t"), \
                mock.patch("diaktoros.config.host_guards.guard_network"):
            trusted_fetch._base_head(LOOP, "o/r", HEAD, "reader")
        big = mock.MagicMock(status=200, headers={"Content-Length": str(755390)})
        big.__enter__.return_value = big
        with mock.patch("urllib.request.urlopen", return_value=big), \
                mock.patch("diaktoros.gh.token", return_value="t"), \
                mock.patch("diaktoros.config.host_guards.guard_network"), \
                self.assertRaises(trusted_fetch.FetchDenied):
            trusted_fetch._base_head(LOOP, "o/r", HEAD, "reader")


if __name__ == "__main__":
    unittest.main()
