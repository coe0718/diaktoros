"""#1349: the ``normalize`` steps no other test reached — ``reviewer_seat``, the ``adjudicator``
block and the ``roots`` rule. Skipping either step must fail a test."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diaktoros import config  # noqa: E402


def _raw(reviewer: dict | None = None, **extra) -> dict:
    return {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
            "read_token": "reader",
            "seats": {"reviewer": {"profile": "rev", "route": "r", **(reviewer or {})},
                      "fixer": {"profile": "fix", "route": "f"}},
            **extra}


class ReviewerSeat(unittest.TestCase):
    def test_defaults_to_the_reviewer_seats_login(self):
        self.assertEqual(config.normalize(_raw())["reviewer_seat"], "reviewer")
        self.assertEqual(config.normalize(_raw({"login": "Rev-Bot"}))["reviewer_seat"], "rev-bot")

    def test_a_named_one_wins_and_is_lowercased(self):
        self.assertEqual(config.normalize(_raw(reviewer_seat="Other"))["reviewer_seat"], "other")

    def test_none_at_all_is_refused(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.normalize(_raw({"login": ""}))
        self.assertIn("'reviewer_seat' (or seats.reviewer.login) is required", str(cm.exception))


class Adjudicator(unittest.TestCase):
    def test_a_profile_only_block_is_dropped(self):
        # what the normalized form writes for "no adjudicator": it must read back as none
        self.assertEqual(config.normalize(_raw(adjudicator={"profile": "default"}))["adjudicator"], {})

    def test_a_block_with_settings_and_no_route_is_refused(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.normalize(_raw(adjudicator={"profile": "arb", "login": "arbiter"}))
        self.assertIn("adjudicator.route is required", str(cm.exception))

    def test_the_profile_defaults(self):
        self.assertEqual(config.normalize(_raw(adjudicator={"route": "a"}))["adjudicator"],
                         {"route": "a", "profile": "default"})


class Roots(unittest.TestCase):
    def test_the_filesystem_root_and_home_are_refused(self):
        home = pathlib.Path.home()
        for root, why in (("/", "the filesystem root"), (str(home), "the home directory"),
                          (str(home.parent), "an ancestor of the home directory")):
            with self.subTest(root=root), self.assertRaises(config.ConfigError) as cm:
                config.normalize(_raw(roots=[root]))
            self.assertIn(f"is {why}; cleanup deletes PR-named children", str(cm.exception))

    def test_a_dedicated_directory_loads(self):
        root = pathlib.Path.home() / "worktrees"
        self.assertEqual(config.normalize(_raw(roots=[root]))["roots"], [str(root)])


if __name__ == "__main__":
    unittest.main()
