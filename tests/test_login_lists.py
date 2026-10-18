"""#782: ``fixers`` / ``reviewers`` must be a list of logins; a string is a ConfigError.

A JSON string used to be iterated character by character ("rev-bot" -> "r", "e", ...), which
passed the "at least one login" check and gave the reviewer seat the login "r".
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import config  # noqa: E402


class LoginLists(unittest.TestCase):
    RAW = {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"], "read_token": "r",
           "tokens": {}, "seats": {"reviewer": {"profile": "r", "route": "w-r"},
                                   "fixer": {"profile": "f", "login": "fix", "route": "w-f"}}}

    def test_a_string_is_refused(self):
        for key in ("fixers", "reviewers"):
            for bad in ("rev-bot", "a,b", {"a": 1}, 5, True, ["ok", 3], [["nested"]]):
                with self.subTest(key=key, bad=bad), self.assertRaises(config.ConfigError) as cm:
                    config.normalize({**self.RAW, key: bad})
                self.assertIn(f"'{key}' must be a list", str(cm.exception))

    def test_the_reviewer_login_is_not_a_character(self):
        with self.assertRaises(config.ConfigError):
            config.normalize({**self.RAW, "reviewers": "rev-bot"})
        loop = config.normalize({**self.RAW, "reviewers": ["Rev-Bot"]})
        self.assertEqual(loop["reviewers"], ["rev-bot"])
        self.assertEqual(loop["seats"]["reviewer"]["login"], "rev-bot")

    def test_empty_or_missing_still_says_at_least_one(self):
        for key in ("fixers", "reviewers"):
            for empty in ([], None):
                with self.subTest(key=key, empty=empty), self.assertRaises(config.ConfigError) as cm:
                    config.normalize({**self.RAW, key: empty})
                self.assertIn("at least one", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
