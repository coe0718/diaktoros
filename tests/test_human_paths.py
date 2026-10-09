"""#478 human_paths: the loop's APPROVE of a diff touching a listed path is refused, with one notice.

The refusal is driven through the live ``RunBroker`` (the enforcing entry point) over its socket;
GitHub and the notice route are fakes. REQUEST_CHANGES and unlisted paths are unaffected.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import config, gh, observer, routes  # noqa: E402
import run_tests as t  # noqa: E402
import test_partial_view_no_approve as pv  # noqa: E402
from diaktoros import cli  # noqa: E402

FILES = f"/repos/{pv.REPO}/pulls/7/files?per_page=100"


class Setting(unittest.TestCase):
    RAW = {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"], "read_token": "r",
           "tokens": {}, "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-r"},
                                   "fixer": {"profile": "f", "login": "fix", "route": "w-f"}}}

    def test_default_validation_and_matching(self):
        self.assertEqual(config.normalize(dict(self.RAW))["human_paths"], [])
        loop = config.normalize({**self.RAW, "human_paths": [".github/**", "scripts/release*"]})
        self.assertEqual(config.human_path_hits(loop, ["src/a.py", ".github/workflows/ci.yml",
                                                       "scripts/release.sh", "scripts/release.sh"]),
                         [".github/workflows/ci.yml", "scripts/release.sh"])
        for bad in (".github", [".github/**", ".github/**"], [""], [3], ["a\nb"]):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize({**self.RAW, "human_paths": bad})

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("human_paths", config.apply_settings(dict(self.RAW), {}))
        got = config.apply_settings(dict(self.RAW), {"human_paths": ".github/**, release/*"})
        self.assertEqual(got["human_paths"], [".github/**", "release/*"])
        self.assertIn("human_paths", config.SETTINGS_SCHEMA if hasattr(config, "SETTINGS_SCHEMA")
                      else Path(config.__file__).parents[1].joinpath("plugin.yaml").read_text())


class Broker(pv.Broker):
    files = [{"filename": "src/lib.rs"}]

    def setUp(self):
        super().setUp()
        self.loop.update(id="w", host="https://owner.example", human_paths=[".github/**"],
                         observer={"route": "w-observe", "profile": "default",
                                   "deliver": "telegram"})
        subs = self.root / "subs.json"
        subs.write_text(json.dumps({"w-observe": {"host": "https://owner.example", "secret": "s",
                                                  **observer.route_contract(self.loop)}}))
        for patch in (mock.patch.dict("os.environ", {"DIAKTOROS_SUBS": str(subs)}),
                      mock.patch.object(gh, "fetch", side_effect=self.fetch)):
            patch.start()
            self.addCleanup(patch.stop)
        fire = mock.patch.object(routes, "fire", return_value=mock.MagicMock(ok=True))
        self.fire = fire.start()
        self.addCleanup(fire.stop)

    def fetch(self, loop, path, method="GET", body=None, login=None):
        if path == FILES or path.startswith(FILES + "&page="):
            if self.files is None:
                return None, "HTTP 500"
            page = int(path.rsplit("=", 1)[1]) if "&page=" in path else 1
            return self.files[(page - 1) * 100:page * 100], ""
        return None, "unexpected " + path

    def notices(self):
        return [c.args[2]["_observer"] for c in self.fire.call_args_list
                if c.args[2].get("_observer", {}).get("event") == "human_paths"]

    def approve(self):
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        return server, self.send(server, "APPROVE")

    def test_a_listed_path_refuses_approve_with_one_notice_and_request_changes_goes_through(self):
        self.files = [{"filename": "src/lib.rs"}, {"filename": ".github/workflows/ci.yml"}]
        server, refused = self.approve()
        self.assertFalse(refused["ok"])
        self.assertIn(".github/workflows/ci.yml", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)
        again = self.send(server, "APPROVE")           # same head: refused again, no second notice
        self.assertFalse(again["ok"])
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(".github/workflows/ci.yml", self.notices()[0]["message"])
        self.assertTrue(self.send(server, "REQUEST_CHANGES", "no\nNot verified: nothing")["ok"])
        self.assertEqual([p["event"] for p in self.posts], ["REQUEST_CHANGES"])

    def test_a_renamed_away_listed_path_counts(self):
        self.files = [{"filename": "docs/ci.yml", "previous_filename": ".github/ci.yml"}]
        self.assertFalse(self.approve()[1]["ok"])

    def test_other_paths_approve_as_before(self):
        server, got = self.approve()
        self.assertTrue(got["ok"], got)
        self.assertEqual([p["event"] for p in self.posts], ["APPROVE"])
        self.assertEqual(self.notices(), [])

    def test_no_list_never_reads_the_files(self):
        self.loop["human_paths"] = []
        self.files = None                                  # would refuse if it were read
        self.assertTrue(self.approve()[1]["ok"])

    def test_a_listing_at_githubs_cap_refuses_approve(self):
        self.files = [{"filename": f"src/f{i}.py"} for i in range(gh.PR_FILES_LISTING_LIMIT)]
        server, got = self.approve()
        self.assertFalse(got["ok"])
        self.assertIn("limit", got["error"])
        self.assertEqual(self.posts, [])

    def test_an_unreadable_file_list_refuses_approve(self):
        self.files = None
        server, got = self.approve()
        self.assertFalse(got["ok"])
        self.assertIn("could not be read", got["error"])
        self.assertEqual(self.posts, [])


LOOP_ID = "humans"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None):
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None):
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> list:
        return json.loads(LOOP_FILE.read_text()).get("human_paths", [])

    def test_init_from_the_form_and_the_flags_win(self):
        form = {"human_paths": ".github/**, release/*"}
        for extra, settings, want in (((), None, []), ((), form, [".github/**", "release/*"]),
                                      (("--human-path", "ci/**"), form, ["ci/**"])):
            with self.subTest(extra=extra, settings=settings):
                LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--human-path", ".github/**",
                           "--human-path", "release/*")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [".github/**", "release/*"])
        rc, out = self.cli("set", "--loop", LOOP_ID, "--human-path", "a", "--human-path", "a")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), [".github/**", "release/*"])
        rc, out = self.cli("set", "--loop", LOOP_ID, "--no-human-paths")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [])
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"human_paths": "ci/**"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), ["ci/**"])

    def test_setup_hands_init_each_pattern(self):
        args = t.parser_for({"human_paths": ".github/**, release/*"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--human-path")],
                         ["--human-path=.github/**", "--human-path=release/*"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--human-path", "ci/**"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--human-path")], ["--human-path=ci/**"])


if __name__ == "__main__":
    unittest.main()
