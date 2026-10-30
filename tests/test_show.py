"""`hermes dk show` (#555): every setting, its value and where it came from."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import unittest

from diaktoros import cli, config, observer, show
from diaktoros.config import blocks  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from test_token_file_settings import TokenFileSettingsTests as _Base, SENTINEL


class ShowTests(_Base):
    # Reuse the fixture (temp home, token files, init argv); only our own tests run here.
    def shown(self, *extra, settings=None):
        self.install()
        return self.run_cli(["show", "--loop", "widgets", *extra], settings)

    def rows(self, settings=None):
        rc, out = self.shown("--json", settings=settings)
        self.assertEqual(rc, 0, out)
        return {r["name"]: r for r in json.loads(out)["settings"]}

    def test_every_schema_key_is_shown(self):
        rows = self.rows()
        missing = set(settings_form.SETTINGS_SCHEMA) - set(rows)
        self.assertFalse(missing, f"settings schema keys `show` does not list: {sorted(missing)}")

    def test_app_schema_rows_are_effective_id_and_key_metadata(self):
        from test_token_file_settings import REV
        self.install()
        raw = self.loop_json()
        bot = "review-seat[bot]"
        raw["reviewers"] = [bot]
        raw["reviewer_seat"] = bot
        raw["seats"]["reviewer"]["login"] = bot
        raw["tokens"].pop(REV)
        raw["apps"] = {bot: {"app_id": 42, "key_file": str(self.pats["rev"])}}
        self.loop_file().write_text(json.dumps(raw))
        rc, out = self.run_cli(["show", "--loop", "widgets", "--json"])
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in json.loads(out)["settings"]}
        self.assertEqual(rows["reviewer_app_id"]["value"], 42)
        self.assertEqual(rows["reviewer_app_key_file"]["value"], str(self.pats["rev"]))
        self.assertNotIn(SENTINEL, json.dumps(rows))

    def test_an_applied_tilde_app_key_path_is_not_called_unapplied(self):
        # #1212: apply stores the key path expanded, so a form path written with ~ is the same
        # file, not a setting "the form holds but the loop does not".
        import pathlib
        from test_token_file_settings import REV
        self.install()
        key = pathlib.Path.home() / "rev-app.pem"
        key.write_text("not a key")
        key.chmod(0o600)
        raw = self.loop_json()
        bot = "review-seat[bot]"
        raw["reviewers"] = [bot]
        raw["reviewer_seat"] = bot
        raw["seats"]["reviewer"]["login"] = bot
        raw["tokens"].pop(REV)
        raw["apps"] = {bot: {"app_id": 42, "key_file": str(key)}}
        self.loop_file().write_text(json.dumps(raw))
        form = {"reviewer_app_id": "42", "reviewer_app_key_file": "~/rev-app.pem"}
        rc, out = self.run_cli(["show", "--loop", "widgets", "--json"], form)
        self.assertEqual(rc, 0, out)
        rows = {row["name"]: row for row in json.loads(out)["settings"]}
        self.assertNotIn("not applied", rows["reviewer_app_key_file"]["note"])
        form["reviewer_app_key_file"] = "~/other.pem"                  # a different file is
        rc, out = self.run_cli(["show", "--loop", "widgets", "--json"], form)
        rows = {row["name"]: row for row in json.loads(out)["settings"]}
        self.assertIn("not applied", rows["reviewer_app_key_file"]["note"])

    def test_every_loop_key_is_shown(self):
        rows = self.rows()
        covered = {n.split(".")[0] for n in rows}
        # Every documented loop key is a row or the root of nested rows; the nested blocks list
        # every key their normalizer accepts.
        unshown = set(config.DEFAULTS) - set(rows) - covered
        self.assertFalse(unshown, f"DEFAULTS keys `show` does not list: {sorted(unshown)}")
        for key in config.TRIAGE_KEYS:
            self.assertTrue(f"triage.{key}" in rows or key in rows, f"triage.{key}")
        for key in show.OBSERVER_KEYS:     # a form field keeps the form's name (observer_<key>)
            self.assertTrue(f"observer.{key}" in rows or f"observer_{key}" in rows, key)
        self.assertIn("adjudicator.route", rows)

    def test_observer_keys_match_the_normalizer(self):
        norm = blocks.normalize_observer({"route": "r", "events": ["stall"], "digest_min": 5,
                                          "weekly_report": "mon 09:00",
                                          "urgent_route": "u", "urgent_profile": "p",
                                          "urgent_deliver": "d"})
        self.assertLessEqual(set(norm), set(show.OBSERVER_KEYS))

    def test_json_round_trips(self):
        self.install()
        rc, out = self.run_cli(["show", "--loop", "widgets", "--json"])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["loop"], "widgets")
        for row in data["settings"]:
            self.assertEqual(set(row), {"name", "value", "source", "meaning", "state", "note"})
            self.assertIn(row["source"], {"loop file", "default", "derived"})

    def test_sources_and_off_notes(self):
        rows = self.rows()
        self.assertEqual(rows["review_only_cap"]["source"], "derived")
        self.assertEqual(rows["review_only_cap"]["value"], rows["cap"]["value"])
        self.assertEqual(rows["fix_ci"]["state"], "off")
        self.assertIn("red CI on fixer PRs waits for a review", rows["fix_ci"]["note"])
        self.assertEqual(rows["ci_fix_cap"]["source"], "default")
        self.assertEqual(rows["reviewer_login"]["source"], "loop file")

    def test_every_fixer_push_hint_is_the_command_that_runs(self):
        # Duplication audit (B3): the hint said `hermes dk fixer-push --enable`, which the CLI
        # refuses (no --loop, no --acknowledge-pr-race).
        self.install()
        path = self.loop_file()
        data = json.loads(path.read_text())
        data.update({"fix_ci": True, "fixer_scope_lock": True, "unattended_fixer_push": False})
        path.write_text(json.dumps(data))
        rows = {r["name"]: r for r in json.loads(
            self.run_cli(["show", "--loop", "widgets", "--json"])[1])["settings"]}
        enable = "hermes dk fixer-push --loop widgets --enable --acknowledge-pr-race"
        self.assertEqual(config.fixer_push_enable_command("widgets"), enable)
        for name in ("unattended_fixer_push", "fix_ci", "fixer_scope_lock"):
            with self.subTest(name):
                self.assertIn(f"({enable})", rows[name]["note"])
        # A second copy of the fix_ci note, written after the loop to whichever row was last,
        # called an off fixer_scope_lock "on, but inert" (#1477).
        data["fixer_scope_lock"] = False
        path.write_text(json.dumps(data))
        rows = {r["name"]: r for r in json.loads(
            self.run_cli(["show", "--loop", "widgets", "--json"])[1])["settings"]}
        self.assertIn("inert", rows["fix_ci"]["note"])
        self.assertEqual(rows["fixer_scope_lock"]["state"], "off")
        self.assertNotIn("inert", rows["fixer_scope_lock"]["note"])

    @staticmethod
    def hand_spelled(source: str) -> list[int]:
        """Lines of strings in ``source`` naming ``dk fixer-push``, docstrings aside: a hint the
        operator would type, which must come from ``config.fixer_push_enable_command``."""
        import ast
        tree = ast.parse(source)
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and node.body and isinstance(node.body[0], ast.Expr)}
        return [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs and "dk fixer-push" in node.value]

    def test_no_hint_spells_the_fixer_push_command_by_hand(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        home = root / "diaktoros" / "config" / "__init__.py"
        for path in [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").rglob("*.py")]:
            if path != home:
                with self.subTest(path.relative_to(root).as_posix()):
                    self.assertEqual(self.hand_spelled(path.read_text()), [])
        # #1477 review: the bare command was one of the broken hints; a docstring is not a hint.
        for hint in ('"(hermes dk fixer-push)"', 'f"run `hermes dk fixer-push --loop {x} --enable`"',
                     '"hermes dk " "fixer-push --enable"'):
            with self.subTest(hint):
                self.assertEqual(self.hand_spelled(f'"""``hermes dk fixer-push``."""\nnote = {hint}\n'), [2])

    def test_a_loop_file_value_wins_and_text_groups_on_first(self):
        self.install()
        path = self.loop_file()
        data = json.loads(path.read_text())
        data.update({"fix_ci": True, "unattended_fixer_push": True, "cap": 5,
                     "review_only_cap": 2,
                     "observer": {"route": "feed", "events": ["opened", "verdict"]}})
        path.write_text(json.dumps(data))
        rc, out = self.run_cli(["show", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertLess(out.index("\non\n"), out.index("off — would matter"))
        rows = {r["name"]: r for r in json.loads(
            self.run_cli(["show", "--loop", "widgets", "--json"])[1])["settings"]}
        self.assertEqual((rows["fix_ci"]["value"], rows["fix_ci"]["state"]), (True, "on"))
        self.assertEqual((rows["review_only_cap"]["value"], rows["review_only_cap"]["source"]),
                         (2, "loop file"))
        self.assertIn("stall", rows["observer.events"]["note"])
        self.assertEqual(set(rows["observer.events"]["note"].split("hides: ")[1].split(", ")),
                         set(observer.EVENTS) - {"opened", "verdict"})

    def test_form_value_is_a_note_not_the_effective_value(self):
        rows = self.rows({"fix_ci": True})
        self.assertIs(rows["fix_ci"]["value"], False)
        self.assertIn("settings form holds", rows["fix_ci"]["note"])

    def test_token_values_are_never_printed_and_nothing_is_written(self):
        self.install()
        before = self.loop_file().read_bytes()
        for argv in (["show", "--loop", "widgets"], ["show", "--loop", "widgets", "--json"]):
            rc, out = self.run_cli(argv)
            self.assertEqual(rc, 0, out)
            self.assertNotIn(SENTINEL, out)
            self.assertIn(str(self.pats["rev"]), out)         # the path, not the token
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_unknown_loop_is_refused(self):
        rc, out = self.run_cli(["show", "--loop", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot show loop", out)


# The inherited tests would run again under this class; keep only ours.
for _name in [n for n in dir(_Base) if n.startswith("test_")]:
    if _name not in ShowTests.__dict__:
        setattr(ShowTests, _name, None)

if __name__ == "__main__":
    unittest.main()
