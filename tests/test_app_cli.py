"""GitHub App identities through the operator's paths (#1163 M1c-1a).

``--app <slug>[bot]=APP_ID:/abs/key.pem`` gives a login a GitHub App identity instead of a PAT,
on ``init`` (any identity), ``setup`` (forwarded to init; that login's token file is not asked)
and ``set`` (the reader or the adjudicator login, as ``--token``; a seat's credential moves
through the settings and ``apply``). The key file passes the token-file checks and is never
read. ``apply`` counts an App reader, and the form's token file never lands on an App seat.
``status``/``init`` credential lines and ``show`` name the App and its key path, never a value.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import cli, show  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.cli import install  # noqa: E402

BOT_REV, BOT_READ = "acme-reviewer[bot]", "acme-reader[bot]"


class Base(unittest.TestCase):
    # Borrow the harness (temp home, gh stub, profiles, PAT files, CLI runner), not its tests.
    setUp = tfs.TokenFileSettingsTests.setUp
    pat = tfs.TokenFileSettingsTests.pat
    run_cli = tfs.TokenFileSettingsTests.run_cli
    loop_file = tfs.TokenFileSettingsTests.loop_file
    loop_json = tfs.TokenFileSettingsTests.loop_json
    assert_no_leak = tfs.TokenFileSettingsTests.assert_no_leak
    assert_refused = tfs.TokenFileSettingsTests.assert_refused

    def key(self, name, mode=0o600):
        return self.pat(f"{name}-app", mode)          # its contents carry the leak sentinel

    def app_argv(self, *extra, reviewer=BOT_REV, reader=tfs.READER, apps=(), tokens=None):
        argv = ["init", "--repo", "acme/widgets", "--fixer", tfs.FIX, "--reviewer", reviewer,
                "--reviewer-profile", "critic", "--fixer-profile", "coder",
                "--read-token", reader, "--host", "https://gateway.example"]
        for login, path in (tokens if tokens is not None else
                            [(tfs.READER, self.pats["read"]), (tfs.FIX, self.pats["fix"])]):
            argv += ["--token", f"{login}={path}"]
        for login, app_id, key in apps:
            argv += ["--app", f"{login}={app_id}:{key}"]
        return argv + list(extra)


class Init(Base):
    def test_a_seat_can_be_a_github_app(self):
        key = self.key("rev")
        rc, out = self.run_cli(self.app_argv(apps=[(BOT_REV, 5416187, key)]))
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(loop["apps"], {BOT_REV: {"app_id": 5416187, "key_file": str(key)}})
        self.assertNotIn(BOT_REV, loop["tokens"])
        self.assertIn(f"reviewer {BOT_REV} → App 5416187, key {key}", out)
        self.assert_no_leak()                          # the key is never read or printed

    def test_the_reader_can_be_a_github_app(self):
        key = self.key("read")
        rc, out = self.run_cli(self.app_argv(
            reviewer=tfs.REV, reader=BOT_READ, apps=[(BOT_READ, 5416115, key)],
            tokens=[(tfs.REV, self.pats["rev"]), (tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_json()["read_token"], BOT_READ)
        self.assertIn(f"read {BOT_READ} → App 5416115, key {key}", out)

    def test_malformed_apps_are_refused_and_nothing_is_written(self):
        good = self.key("rev")
        loose = self.key("loose", 0o644)
        for pair, why in ((f"{BOT_REV}={good}", "expects"),
                          (f"acme-reviewer=5:{good}", "not an App's bot login"),
                          (f"{BOT_REV}=five:{good}", "not a number"),
                          (f"{BOT_REV}=0:{good}", "not a number"),
                          (f"{BOT_REV}=5:{loose}", "group/other"),
                          (f"{BOT_REV}=5:relative.pem", "absolute|relative")):
            with self.subTest(pair=pair):
                self.assert_refused(self.app_argv() + ["--app", pair], why)

    def test_a_pat_and_an_app_for_one_login_is_refused(self):
        self.assert_refused(self.app_argv(apps=[(BOT_REV, 5, self.key("rev"))],
                                          tokens=[(tfs.READER, self.pats["read"]),
                                                  (tfs.FIX, self.pats["fix"]),
                                                  (BOT_REV, self.pats["rev"])]),
                            "both 'tokens' and 'apps'")

    def test_the_forms_token_file_never_lands_on_an_app_seat(self):
        key = self.key("rev")
        settings = {"reviewer_token_file": str(self.pats["rev"])}
        rc, out = self.run_cli(self.app_argv(apps=[(BOT_REV, 5, key)]), settings=settings)
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertNotIn(BOT_REV, loop["tokens"])
        self.assertIn(BOT_REV, loop["apps"])


class Set(Base):
    def install(self):
        rc, out = self.run_cli(self.app_argv(
            reviewer=tfs.REV, tokens=[(tfs.READER, self.pats["read"]), (tfs.REV, self.pats["rev"]),
                                      (tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)

    def test_the_reader_moves_to_a_github_app(self):
        self.install()
        key = self.key("read")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", BOT_READ,
                                "--app", f"{BOT_READ}=5416115:{key}"])
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(loop["read_token"], BOT_READ)
        self.assertEqual(loop["apps"][BOT_READ], {"app_id": 5416115, "key_file": str(key)})
        self.assert_no_leak()

    def test_a_pat_replaces_an_app_for_the_same_login_and_back(self):
        self.install()
        key = self.key("read")
        self.assertEqual(self.run_cli(["set", "--loop", "widgets", "--read-token", BOT_READ,
                                       "--app", f"{BOT_READ}=1:{key}"])[0], 0)
        newer = self.pat("read2")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", BOT_READ,
                                "--token", f"{BOT_READ}={newer}"])
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertEqual(loop["tokens"][BOT_READ], str(newer))
        self.assertNotIn(BOT_READ, loop.get("apps") or {})
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", BOT_READ,
                                "--app", f"{BOT_READ}=2:{key}"])
        self.assertEqual(rc, 0, out)
        loop = self.loop_json()
        self.assertNotIn(BOT_READ, loop["tokens"])
        self.assertEqual(loop["apps"][BOT_READ]["app_id"], 2)

    def test_a_seats_credential_does_not_move_through_set(self):
        rc, out = self.run_cli(self.app_argv(apps=[(BOT_REV, 5, self.key("rev"))]))
        self.assertEqual(rc, 0, out)
        self.assert_refused(["set", "--loop", "widgets", "--app", f"{BOT_REV}=6:{self.key('r2')}"],
                            "seat or the current reader")

    def test_a_malformed_app_is_refused(self):
        self.install()
        self.assert_refused(["set", "--loop", "widgets", "--read-token", BOT_READ,
                             "--app", f"{BOT_READ}=x:{self.key('read')}"], "not a number")


class ApplyAndShow(Base):
    def install_app_reader(self, extra_apps=()):
        rc, out = self.run_cli(self.app_argv(
            reviewer=BOT_REV, reader=BOT_READ,
            apps=[(BOT_READ, 1, self.key("read")), (BOT_REV, 2, self.key("rev")), *extra_apps],
            tokens=[(tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)

    def test_apply_counts_an_app_reader_and_keeps_an_app_seats_key(self):
        self.install_app_reader()
        before = self.loop_json()
        rc, out = self.run_cli(["apply", "--loop", "widgets"],
                               settings={"reviewer_token_file": str(self.pats["rev"])})
        self.assertNotIn("has no entry in 'tokens'", out)
        self.assertEqual(rc, 0, out)
        after = self.loop_json()
        self.assertEqual(after["apps"], before["apps"])
        self.assertNotIn(BOT_REV, after["tokens"])

    def test_show_names_an_app_seats_key_instead_of_unset(self):
        self.install_app_reader()
        from diaktoros import config
        loop = config.load_id("widgets")
        rows = {r.name: r for r in show._rows(loop)}
        row = rows["reviewer_token_file"]
        self.assertIn("a GitHub App identity: App 2, key", row.note)
        self.assertEqual(row.path, ("apps",))

    def test_status_lines_name_the_app_and_never_say_falls_back(self):
        self.install_app_reader()
        from diaktoros import config
        lines = cli_common._credential_lines(config.load_id("widgets"))
        self.assertTrue(any(line.startswith(f"reviewer {BOT_REV} → App 2, key ") for line in lines))
        self.assertTrue(any(line.startswith(f"read {BOT_READ} → App 1, key ") for line in lines))
        self.assertFalse(any("no file mapped" in line for line in lines), lines)


class Setup(Base):
    def argv(self, *flags):
        ctx = tfs._Ctx()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes dk")
        ctx.setup(parser)
        return parser.parse_args(["setup", "--repo", "acme/widgets", *flags])

    def test_an_app_login_is_not_asked_for_a_token_file_and_the_app_is_forwarded(self):
        key = self.key("rev")
        args = self.argv("--reviewer", BOT_REV, "--fixer", tfs.FIX, "--reviewer-profile", "critic",
                         "--fixer-profile", "coder", "--read-token", tfs.READER,
                         "--read-token-file", str(self.pats["read"]),
                         "--fixer-token", str(self.pats["fix"]),
                         "--host", "https://gateway.example",
                         "--app", f"{BOT_REV}=5416187:{key}")
        asked = []
        orig = install._ask
        install._ask = lambda question, default="", interactive=False: \
            asked.append(question) or default
        self.addCleanup(setattr, install, "_ask", orig)
        argv, _admin = install._setup_init_argv(args, "acme/widgets", "widgets", interactive=False)
        self.assertIn(f"--app={BOT_REV}=5416187:{key}", argv)
        self.assertFalse(any(a.startswith(f"--token={BOT_REV}=") for a in argv), argv)
        self.assertFalse(any(f"token file for {BOT_REV}" in q for q in asked), asked)
        self.assertIn(f"--token={tfs.FIX}={self.pats['fix']}", argv)


if __name__ == "__main__":
    unittest.main()
