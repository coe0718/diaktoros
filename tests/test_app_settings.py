"""App form selection, #1197, and credential-only apply safety rails."""
import _home_guard  # noqa: F401
from unittest.mock import patch

from test_app_cli import Base, BOT_REV
from diaktoros import config
from diaktoros.cli import common as cli_common
from diaktoros.config import settings_form


class AppSettings(Base):
    def install(self):
        self.key1 = self.key("rev")
        rc, out = self.run_cli(self.app_argv(apps=[(BOT_REV, 2, self.key1)]))
        self.assertEqual(rc, 0, out)

    def test_1197_missing_stale_pat_is_ignored_with_note(self):
        self.install()
        before = self.loop_file().read_bytes()
        rc, out = self.run_cli(["apply", "--loop", "widgets"],
                               {"reviewer_token_file": str(self.keys / "gone.pat")})
        self.assertEqual(rc, 0, out)
        self.assertIn("reviewer_token_file is not applied", out)
        self.assertIn("stale", out)
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_id_and_key_changes_are_busy_identity_changes(self):
        self.install()
        for app_id, key in (("3", self.key1), ("2", self.key("new"))):
            with self.subTest(app_id=app_id, key=key):
                settings = {"reviewer_app_id": app_id, "reviewer_app_key_file": str(key)}
                before = self.loop_file().read_bytes()
                with patch.object(cli_common, "_busy_seats", return_value=["reviewer in flight"]) as busy:
                    self.assert_refused(["apply", "--loop", "widgets"], "--while-busy", settings)
                    self.assertIn("reviewer", busy.call_args.args[1])
                self.assertEqual(before, self.loop_file().read_bytes())

    def test_app_tuple_partial_and_non_ascii_id_refuse(self):
        self.install()
        for settings in ({"reviewer_app_id": "3"},
                         {"reviewer_app_key_file": str(self.key1)},
                         *({"reviewer_app_id": value, "reviewer_app_key_file": str(self.key1)}
                           for value in ("0", "-1", "+2", "٢", "2.0"))):
            with self.subTest(settings=settings):
                self.assert_refused(["apply", "--loop", "widgets"], "reviewer_app", settings)

    def test_dry_run_shows_app_kind_id_and_path_without_writing(self):
        self.install()
        key = self.key("new")
        settings = {"reviewer_app_id": "3", "reviewer_app_key_file": str(key)}
        before = self.loop_file().read_bytes()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"], settings)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"App 3, key {key}", out)
        self.assertEqual(self.loop_file().read_bytes(), before)
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["apps"][BOT_REV], {"app_id": 3, "key_file": str(key)})
        self.assert_no_leak()

    def test_init_uses_form_app_tuple_and_explicit_app_ignores_missing_pat(self):
        key = self.key("form")
        settings = {"reviewer_app_id": "7", "reviewer_app_key_file": str(key)}
        rc, out = self.run_cli(self.app_argv(), settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["apps"][BOT_REV], {"app_id": 7, "key_file": str(key)})
        self.loop_file().unlink()
        rc, out = self.run_cli(self.app_argv(apps=[(BOT_REV, 8, key)]),
                               {"reviewer_token_file": str(self.keys / "missing.pat")})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["apps"][BOT_REV]["app_id"], 8)

    def test_explicit_pat_app_conflict_refuses_but_matching_existing_pat_is_stale(self):
        from test_token_file_settings import READER, FIX
        # A bot login can have a PAT before an explicit App migration.
        rc, out = self.run_cli(self.app_argv(tokens=[(READER, self.pats["read"]),
            (FIX, self.pats["fix"]), (BOT_REV, self.pats["rev"])]))
        self.assertEqual(rc, 0, out)
        key = self.key("migration")
        settings = {"reviewer_app_id": "7", "reviewer_app_key_file": str(key),
                    "reviewer_token_file": str(self.pats["rev2"])}
        self.assert_refused(["apply", "--loop", "widgets"], "choose PAT or App", settings)
        settings["reviewer_token_file"] = str(self.pats["rev"])
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 0, out)
        self.assertIn("stale PAT setting", out)
        self.assertNotIn(BOT_REV, self.loop_json()["tokens"])
        self.assertEqual(self.loop_json()["apps"][BOT_REV]["app_id"], 7)
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 0, out)  # The unchanged accepted migration form remains a no-op.
        # A form PAT never silently downgrades the resulting App.
        rc, out = self.run_cli(["apply", "--loop", "widgets"],
                               {"reviewer_token_file": str(self.pats["rev2"])})
        self.assertEqual(rc, 0, out)
        self.assertNotIn(BOT_REV, self.loop_json()["tokens"])

    def test_intended_missing_pat_still_refuses(self):
        from test_token_file_settings import READER, REV, FIX
        rc, out = self.run_cli(self.app_argv(reviewer=REV, tokens=[
            (READER, self.pats["read"]), (REV, self.pats["rev"]), (FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)
        self.assert_refused(["apply", "--loop", "widgets"], "reviewer_token_file: .*does not exist",
                            {"reviewer_token_file": str(self.keys / "missing.pat")})

    def test_adjudicator_app_changes_are_written_without_rebinding_comment_identity(self):
        self.install()
        bot = "arbiter[bot]"
        key = self.key("adj")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "arbiter"])
        self.assertEqual(rc, 0, out)
        settings = {"adjudicator_login": bot, "adjudicator_app_id": "9",
                    "adjudicator_app_key_file": str(key)}
        with patch.object(cli_common, "_busy_seats", return_value=[]) as busy:
            rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 0, out)
        busy.assert_not_called()
        self.assertIn(f"App 9, key {key}", out)
        self.assertEqual(self.loop_json()["apps"][bot]["app_id"], 9)
        settings["adjudicator_app_id"] = "10"
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["apps"][bot]["app_id"], 10)
        rc, out = self.run_cli(["apply", "--loop", "widgets"],
                               {"adjudicator_token_file": str(self.keys / "missing.pat")})
        self.assertEqual(rc, 0, out)
        self.assertIn("adjudicator_token_file is not applied", out)

    def test_no_adjudicator_route_leaves_form_identity_inert(self):
        self.install()
        before = self.loop_file().read_bytes()
        rc, out = self.run_cli(["apply", "--loop", "widgets"],
            {"adjudicator_login": "arbiter[bot]", "adjudicator_app_id": "9",
             "adjudicator_app_key_file": str(self.keys / "missing.pem")})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_key_change_still_uses_route_ownership_preflight(self):
        self.install()
        settings = {"reviewer_app_id": "2", "reviewer_app_key_file": str(self.key("new"))}
        with patch.object(cli_common, "_verify_routes", side_effect=config.ConfigError("foreign route")) as verify:
            self.assert_refused(["apply", "--loop", "widgets"], "foreign route", settings)
            self.assertIn("reviewer", verify.call_args.args[1])
        with patch.object(cli_common, "_busy_seats", return_value=["reviewer in flight"]):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--while-busy"], settings)
        self.assertEqual(rc, 0, out)
        self.assertIn("--while-busy", out)
        self.assertEqual(self.loop_json()["apps"][BOT_REV]["key_file"], settings["reviewer_app_key_file"])

    def test_all_roles_blank_preserve_and_app_drops_case_variant_pats(self):
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                bot = f"{role}[bot]"
                key = self.key(role)
                raw = {"seats": {role: {"login": bot}}, "adjudicator": {"route": "breach"},
                       "tokens": {bot.upper(): "/old.pat", bot: "/old.pat"}}
                blank = settings_form.apply_seats(raw, {f"{role}_app_id": " ",
                                                        f"{role}_app_key_file": ""})
                self.assertEqual(blank, raw)
                selected = settings_form.apply_seats(raw, {f"{role}_app_id": "3",
                                                           f"{role}_app_key_file": str(key)})
                self.assertEqual(selected["tokens"], {})
                self.assertEqual(selected["apps"], {bot: {"app_id": 3, "key_file": str(key)}})
                self.assertEqual(raw["tokens"], {bot.upper(): "/old.pat", bot: "/old.pat"})

    def test_selected_key_metadata_is_checked_without_reading(self):
        self.install()
        for key in (self.keys / "gone.pem", self.key("loose", 0o644)):
            self.assert_refused(["apply", "--loop", "widgets"], "reviewer_app_key_file",
                                {"reviewer_app_id": "3", "reviewer_app_key_file": str(key)})
        with patch.object(type(self.key1), "read_text", side_effect=AssertionError("read")), \
                patch("builtins.open", side_effect=AssertionError("open")):
            selected = settings_form.select_credentials({"seats": {"reviewer": {"login": BOT_REV}}},
                    {"reviewer_app_id": "3", "reviewer_app_key_file": str(self.key1)})
            from diaktoros.config import credentials
            credentials.verify_token_settings({"reviewer_app_id": "3", "reviewer_app_key_file": str(self.key1)},
                loop={"seats": {"reviewer": {"login": BOT_REV}}})
            self.assertEqual(selected[1][BOT_REV]["app_id"], 3)
