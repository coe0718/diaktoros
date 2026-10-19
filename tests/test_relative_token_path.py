"""A relative ``tokens`` path fails at load (#990), with the wording every other token check uses."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import tempfile
import unittest

from diaktoros import config
from diaktoros.config import credentials


def raw_loop(tokens):
    return {"id": "widgets", "repo": "owner/widgets", "fixers": ["fixer"],
            "reviewers": ["reviewer"], "read_token": "reader", "tokens": tokens,
            "seats": {"reviewer": {"route": "widgets-review", "profile": "reviewer"},
                      "fixer": {"route": "widgets-fix", "profile": "fixer"}}}


class RelativeTokenPath(unittest.TestCase):
    def test_normalize_refuses_a_relative_token_path(self):
        with self.assertRaisesRegex(config.ConfigError,
                                    r"tokens\.rev-bot: 'pat' is not an absolute path "
                                    r"\(use /… or ~/…\)"):
            config.normalize(raw_loop({"reader": "/abs/reader.pat", "rev-bot": "pat"}))

    def test_load_file_refuses_a_hand_edited_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "widgets.json"
            path.write_text(json.dumps(raw_loop({"reader": "keys/reader.pat"})))
            with self.assertRaisesRegex(config.ConfigError, "not an absolute path"):
                config.load_file(path)

    def test_absolute_and_home_paths_still_load(self):
        loop = config.normalize(raw_loop({"reader": "/abs/reader.pat",
                                          "rev-bot": "~/keys/rev.pat"}))
        self.assertEqual(loop["tokens"]["rev-bot"], "~/keys/rev.pat")

    def test_one_wording_for_load_and_doctor_credential_check(self):
        self.assertEqual(credentials.token_file_problem("pat"),
                         config.relative_path_problem("pat"))


if __name__ == "__main__":
    unittest.main()
