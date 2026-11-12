"""doctor, selftest and backup on a GitHub App loop (#1163 M1c-2; #1181, #1182).

A loop built by ``init --app`` (an App reviewer and an App reader) reads as healthy where it is:
each credential check names the App and its key file (exists, private; never opened) and the
host's JWT signer, instead of "no token file" with a ``--token`` fix ``check_apps`` would refuse.
Expiry and scopes say an App has neither. selftest resolves each App identity to its bot without
``GET /user``. A backup records the App keys, and a restore names a missing one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import copy
import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import backup, config, doctor, envnames, gh, github_app, selftest  # noqa: E402
from diaktoros.config import credentials, loop_checks  # noqa: E402
from diaktoros.doctor import hook_checks, identity_checks, seat_checks  # noqa: E402

BOT_REV, BOT_READ = app_cli.BOT_REV, app_cli.BOT_READ
BAD = (doctor.ABSENT, doctor.MISMATCH)


class AppLoop(app_cli.Base):
    def setUp(self):
        super().setUp()
        self.rev_key, self.read_key = self.key("rev"), self.key("read")
        rc, out = self.run_cli(self.app_argv(
            reviewer=BOT_REV, reader=BOT_READ,
            apps=[(BOT_READ, 1, self.read_key), (BOT_REV, 2, self.rev_key)],
            tokens=[(tfs.FIX, self.pats["fix"])]))
        self.assertEqual(rc, 0, out)
        self.loop = config.load_id("widgets")
        signer = mock.patch.object(github_app, "signer", return_value=("openssl", Path("/x")))
        signer.start()
        self.addCleanup(signer.stop)


class Doctor(AppLoop):
    def test_an_app_seat_reads_as_verified_with_its_key(self):
        got = identity_checks.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        self.assertIn(f"{BOT_REV} → GitHub App 2, key {self.rev_key}", got.detail)
        self.assertNotIn("--token", got.fix or "")
        self.assertEqual(identity_checks.check_credential(self.loop, "fixer").status, doctor.VERIFIED)

    def test_a_reader_token_in_another_case_reads_as_mapped(self):
        # #1475 review: doctor wanted the exact key while every GitHub call took any case.
        self.loop["read_token"] = tfs.READER
        self.loop["tokens"][tfs.READER.upper()] = str(self.pats["read"])
        got = identity_checks.check_read_token(self.loop)
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        self.assertIn("mapped in tokens", got.detail)

    def test_an_app_reader_and_every_credential_line_pass(self):
        self.assertEqual(identity_checks.check_read_token(self.loop).status, doctor.VERIFIED)
        self.assertIn("a GitHub App", identity_checks.check_read_token(self.loop).detail)
        checks = identity_checks.check_tokens(self.loop)
        self.assertEqual({c.name for c in checks},
                         {f"token:{tfs.FIX}", f"app:{BOT_REV}", f"app:{BOT_READ}"})
        self.assertFalse([c for c in checks if c.status in BAD], [c.detail for c in checks])

    def test_a_loose_or_missing_key_is_named_with_a_key_fix(self):
        self.rev_key.chmod(0o644)
        got = identity_checks.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn(f"chmod 600 {self.rev_key}", got.fix)
        self.rev_key.unlink()
        got = identity_checks.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.ABSENT)
        self.assertIn("private key", got.fix)

    def test_no_signer_is_named(self):
        with mock.patch.object(github_app, "signer", return_value=("", None)):
            got = identity_checks.check_credential(self.loop, "reviewer")
        self.assertEqual(got.status, doctor.ABSENT)
        self.assertIn("cryptography", got.fix)

    def test_expiry_and_scopes_say_an_app_has_neither(self):
        with mock.patch.object(gh, "request", side_effect=AssertionError("no /user for an App")):
            checks = {c.name: c for c in identity_checks.check_token_expiry_scopes(self.loop, offline=True)}
        for login in (BOT_REV, BOT_READ):
            self.assertEqual(checks[f"token-expiry:{login}"].status, doctor.VERIFIED)
            self.assertIn("no PAT expiry", checks[f"token-expiry:{login}"].detail)

    def test_doctor_names_the_loops_own_identities_among_the_fix_authors(self):
        # #1794 (review of #1804, F1): an App reader's and reviewer's issues are fixable; doctor
        # says so, by name, beside triage.authors.
        self.loop["triage"] = {**self.loop.get("triage", {}), "route": "widgets-triage",
                               "authors": ["fix-acct"], "labels": ["bug"]}
        got = {c.name: c for c in seat_checks.check_triage(self.loop)}["triage:fix_authors"]
        self.assertEqual(got.status, doctor.VERIFIED)
        for login in ("fix-acct", BOT_READ.lower(), BOT_REV.lower()):
            self.assertIn(login, got.detail)
        self.assertIn(f"reader {BOT_READ}", got.detail)
        self.assertIn(f"reviewer {BOT_REV}", got.detail)

    def test_an_unmapped_seat_is_told_the_settings_way_for_a_pat_or_an_app(self):
        # #2240: `set --token`/`--app` refuse a seat's login, so the remedy is the settings form.
        self.loop["tokens"].pop(tfs.FIX)
        env = config.profile_dir(config.seat_profile(self.loop, "fixer")) / ".env"
        for gh_token in (False, True):
            if gh_token:
                env.parent.mkdir(parents=True, exist_ok=True)
                env.write_text("GH_TOKEN=x\n")
            got = identity_checks.check_credential(self.loop, "fixer")
            self.assertEqual(got.status, doctor.ABSENT, got.detail)
            self.assertIn(credentials.seat_fix(self.loop, "fixer"), got.fix)
            self.assertIn("fixer_app_id and fixer_app_key_file (a GitHub App)", got.fix)
            self.assertNotIn("--token", got.fix)

    def test_an_unmapped_triage_login_is_told_a_pat_or_an_app(self):
        # #2240: its own login maps with `set --token` or `set --app`; the reviewer's, the seat way.
        self.loop["triage"] = {**self.loop.get("triage", {}), "route": "widgets-triage",
                               "login": "acme-triage", "authors": ["fix-acct"], "labels": ["bug"]}
        got = {c.name: c for c in seat_checks.check_triage(self.loop)}["credential:triage"]
        self.assertEqual(got.status, doctor.ABSENT, got.detail)
        self.assertIn("--token acme-triage=/path/to/pat` (or --app acme-triage=APP_ID:", got.fix)
        self.loop["triage"].pop("login")
        self.loop["apps"].pop(BOT_REV)
        got = {c.name: c for c in seat_checks.check_triage(self.loop)}["credential:triage"]
        self.assertEqual(got.fix, "triage labels as the reviewer seat: "
                         + credentials.seat_fix(self.loop, "reviewer"))

    def test_an_app_triage_login_and_adjudicator_read_as_verified(self):
        self.loop["triage"] = {**self.loop.get("triage", {}), "route": "widgets-triage",
                               "authors": ["fix-acct"], "labels": ["bug"]}
        triage = {c.name: c for c in seat_checks.check_triage(self.loop)}["credential:triage"]
        self.assertEqual(triage.status, doctor.VERIFIED, triage.detail)
        arbiter = self.key("adj")
        self.loop["apps"]["acme-arbiter[bot]"] = {"app_id": 3, "key_file": str(arbiter)}
        self.loop["seats"]["adjudicator"] = {"login": "acme-arbiter[bot]"}
        got = identity_checks.check_adjudicator_identity(self.loop)
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        # An App's credential is its key, never "its own token file" (#1687, #1675).
        self.assertIn("acme-arbiter[bot] → its own GitHub App key", got.detail)
        self.assertNotIn("token file", got.detail)
        self.loop["apps"]["acme-arbiter[bot]"]["key_file"] = str(self.rev_key)
        got = identity_checks.check_adjudicator_identity(self.loop)
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn("same GitHub App key as the reviewer", got.detail)
        self.assertIn("its own GitHub App and key", got.fix)
        self.assertNotIn("PAT", got.fix)


class _Answer:
    """What ``urlopen`` returns for an HTTP answer: a status, headers and a body to read."""
    def __init__(self, status, body):
        self.status, self.body, self.headers = status, body, {"X-Foo": "1"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class Live(AppLoop):
    """doctor's live App check (shadow review of #1198): the key signs as this App and the App is
    installed here, read with the App's JWT; no token minted."""

    def live(self, app=None, installation=None, raises=None):
        def as_app(loop, login, path, method="GET", body=None):
            self.assertEqual(method, "GET")             # reads only
            if raises:
                raise raises
            if path == "/app":
                return gh.Response(app, "" if app else "HTTP 401", 200 if app else 401, {})
            return gh.Response(installation, "" if installation else "HTTP 404 Not Found",
                               200 if installation else 404, {})
        with mock.patch.object(github_app, "_as_app", side_effect=as_app), \
                mock.patch.object(github_app, "installation_token",
                                  side_effect=AssertionError("doctor must not mint")):
            return identity_checks.check_app_live(self.loop, BOT_REV, offline=False)

    def test_an_installed_app_is_verified_with_its_installation(self):
        got = self.live({"id": 2, "slug": "acme-reviewer"}, {"id": 173885357})
        self.assertEqual(got.status, doctor.VERIFIED, got.detail)
        self.assertIn("installed on acme/widgets (installation 173885357)", got.detail)

    def test_an_app_not_installed_here_is_absent_with_the_install_fix(self):
        got = self.live({"id": 2, "slug": "acme-reviewer"}, None)
        self.assertEqual(got.status, doctor.ABSENT)
        self.assertIn("not installed on acme/widgets", got.detail)
        self.assertIn("Install App", got.fix)

    def test_a_key_for_another_app_or_a_renamed_app_is_a_mismatch(self):
        got = self.live({"id": 99, "slug": "other"}, {"id": 1})
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn("signs as App 99", got.detail)
        got = self.live({"id": 2, "slug": "renamed"}, {"id": 1})
        self.assertEqual(got.status, doctor.MISMATCH)
        self.assertIn("renamed[bot]", got.detail)

    def test_a_key_that_cannot_sign_or_answer_is_a_mismatch(self):
        self.assertEqual(self.live(raises=github_app.AppError("signing failed")).status,
                         doctor.MISMATCH)
        self.assertEqual(self.live(None, None).status, doctor.MISMATCH)

    def silent(self, which):
        """The live probe when ``which`` (/app, /installation or both) gets no HTTP answer."""
        def as_app(loop, login, path, method="GET", body=None):
            if path == "/app" and which != "installation":
                return gh.Response(None, "URLError: <urlopen error [Errno -2] Name or service not known>")
            if path == "/app":
                return gh.Response({"id": 2, "slug": "acme-reviewer"}, "", 200, {})
            return gh.Response(None, "URLError: <urlopen error timed out>")
        with mock.patch.object(github_app, "_as_app", side_effect=as_app):
            return identity_checks.check_app_live(self.loop, BOT_REV, offline=False)

    def test_a_network_outage_is_unknown_not_a_key_mismatch(self):
        # #1202: no HTTP answer said nothing about the key; the fix must not send the operator
        # to regenerate a working key.
        for which in ("app", "installation"):
            with self.subTest(which):
                got = self.silent(which)
                self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
                self.assertIn("no HTTP answer", got.detail)
                self.assertIn("URLError", got.detail)
                self.assertIn("api.github.com", got.fix)
                self.assertNotIn("private key", got.fix)
                self.assertNotIn("is not installed", got.detail)

    def real(self, call, **urlopen):
        """``call()`` with the real ``_as_app``: only the socket (``urlopen``) and the JWT signer
        are replaced, so the response it builds is the one the check sees."""
        env = {k: v for k, v in os.environ.items() if k not in envnames.both("GH_STUB")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(gh, "API", "http://127.0.0.1:9"), \
                mock.patch.object(github_app, "jwt", return_value="a.b.c"), \
                mock.patch("urllib.request.urlopen", **urlopen):
            return call()

    def live_real(self, **urlopen):
        return self.real(lambda: identity_checks.check_app_live(self.loop, BOT_REV, offline=False),
                         **urlopen)

    def test_the_real_transport_error_reaches_the_check_as_unknown(self):
        # The path the issue names: _as_app's except-Exception branch, then the check.
        got = self.live_real(side_effect=urllib.error.URLError("[Errno -2] Name or service not known"))
        self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
        self.assertIn("URLError", got.detail)

    def test_a_malformed_answer_is_not_reported_as_a_network_outage(self):
        # Review F1 (#1913): a 200 whose body is not JSON fails in _as_app's except-Exception
        # branch AFTER GitHub answered. The check must not say "no HTTP answer" and send the
        # operator to the network: GitHub did answer.
        got = self.live_real(return_value=_Answer(200, b"<html>not json</html>"))
        self.assertNotIn("no HTTP answer", got.detail)
        self.assertNotIn("api.github.com (network, DNS", got.fix)
        self.assertIn("JSONDecodeError", got.detail)

    def test_a_200_with_an_unreadable_body_blames_neither_the_key_nor_the_install(self):
        # #1923: a captive portal or proxy answering 200 with HTML. A 200 means the JWT was
        # accepted (on /app) or the App is installed (on /installation): neither is blamed.
        portal = b"<html>captive portal</html>"
        app = b'{"id": 2, "slug": "acme-reviewer"}'
        for which, answers in (("app", [_Answer(200, portal), _Answer(200, portal)]),
                               ("installation", [_Answer(200, app), _Answer(200, portal)])):
            with self.subTest(which):
                got = self.live_real(side_effect=answers)
                self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
                self.assertIn("GitHub answered 200 but the body could not be read", got.detail)
                self.assertIn("JSONDecodeError", got.detail)
                self.assertIn("captive portal", got.fix)
                self.assertNotIn("private key", got.fix)
                self.assertNotIn("is not installed", got.detail)
                self.assertNotIn("Install App", got.fix)

    def test_a_200_that_is_not_the_expected_object_is_unknown(self):
        # #1923: JSON that parses but is not GitHub's object (a proxy's own JSON) is the same case.
        got = self.live({"id": 2, "slug": "acme-reviewer"}, ["not", "an", "installation"])
        self.assertEqual(got.status, doctor.UNKNOWN, got.detail)
        self.assertIn("not the expected JSON", got.detail)
        self.assertNotIn("is not installed", got.detail)

    def test_as_app_keeps_the_status_of_an_answer_whose_body_cannot_be_read(self):
        # The layer under the check: a missing status means no HTTP answer, never "answered,
        # then failed to read".
        bad = self.real(lambda: github_app._as_app(self.loop, BOT_REV, "/app"),
                        return_value=_Answer(200, b"not json"))
        gone = self.real(lambda: github_app._as_app(self.loop, BOT_REV, "/app"),
                         side_effect=urllib.error.URLError("down"))
        self.assertEqual((bad.status, bad.data, bad.headers), (200, None, {"x-foo": "1"}))
        self.assertIn("JSONDecodeError", bad.error)
        self.assertEqual((gone.status, gone.data), (None, None))
        self.assertIn("URLError", gone.error)

    def test_an_answer_from_github_stays_a_mismatch_or_absent(self):
        # What the fix must not loosen: a status is GitHub's answer about the key or the install.
        self.assertEqual(self.live(None, None).status, doctor.MISMATCH)
        self.assertEqual(self.live({"id": 2, "slug": "acme-reviewer"}, None).status, doctor.ABSENT)

    def test_check_loop_reports_the_outage_as_unknown_per_app(self):
        def as_app(loop, login, path, method="GET", body=None):
            return gh.Response(None, "URLError: <urlopen error [Errno -3] Temporary failure>")
        with mock.patch.object(github_app, "_as_app", side_effect=as_app), \
                mock.patch.object(hook_checks, "gateway_reachable", return_value=(True, "")):
            checks = {c.name: c for c in doctor.check_loop(self.loop, offline=False)
                      if c.name.startswith("app-live:")}
        self.assertEqual({f"app-live:{BOT_REV}", f"app-live:{BOT_READ}"}, set(checks))
        for check in checks.values():
            self.assertEqual(check.status, doctor.UNKNOWN, check.detail)

    def test_offline_says_not_probed_and_check_loop_runs_it_per_app(self):
        self.assertEqual(identity_checks.check_app_live(self.loop, BOT_REV, offline=True).status,
                         doctor.UNKNOWN)
        names = {c.name for c in doctor.check_loop(self.loop, offline=True)}
        self.assertTrue({f"app-live:{BOT_REV}", f"app-live:{BOT_READ}"} <= names, names)


class CaseVariants(AppLoop):
    """#1474: a login finds its credential in any case, exact key first (``util.login_entry``).
    One login under two spellings is refused, so no check can pick a file the lookup does not
    (#1475 review)."""

    def test_one_login_in_two_spellings_is_refused_by_every_check(self):
        roles = {"read", "reviewer", "fixer"}
        # The review's shape: the fixer's lookup takes FIX-ACCT (the reader's file), a check
        # that folds keeps the last, Fix-Acct (its own), so "distinct files" passed.
        reader = copy.deepcopy(self.loop)
        reader.update(read_token=tfs.READER, tokens={tfs.READER: str(self.pats["read"]),
                                                     tfs.FIX.upper(): str(self.pats["read"]),
                                                     tfs.FIX.title(): str(self.pats["fix"])})
        apps = copy.deepcopy(self.loop)
        apps["apps"][BOT_REV.upper()] = dict(apps["apps"][BOT_REV])
        for name, loop, spelled in (("tokens", reader, "tokens.FIX-ACCT and tokens.Fix-Acct"),
                                    ("apps", apps, f"apps.{BOT_REV} and apps.{BOT_REV.upper()}")):
            with self.subTest(name):
                clash = credentials.case_clash(loop)
                self.assertIn(spelled, clash)
                self.assertEqual(credentials.reader_problem(loop), clash)
                for checked in (roles, set()):      # refused even with no role to check
                    with self.assertRaisesRegex(config.ConfigError, "one login in two spellings"):
                        credentials.verify_credentials(copy.deepcopy(loop), checked)
                with self.assertRaisesRegex(config.ConfigError, "one login in two spellings"):
                    loop_checks._check_tokens(copy.deepcopy(loop), "widgets")
        self.assertEqual(credentials.case_clash(self.loop), "")

    def test_seats_sharing_a_file_are_refused_whatever_the_spelling(self):
        # The reviewer/fixer check had no test; it now finds each seat's file by the one lookup.
        loop = copy.deepcopy(self.loop)
        loop["seats"]["fixer"]["login"] = tfs.FIX.title()
        loop["apps"].pop(BOT_REV)
        shared = loop["tokens"].pop(tfs.FIX)
        loop["tokens"].update({tfs.FIX.upper(): shared, BOT_REV: shared})
        with self.assertRaisesRegex(config.ConfigError, "the reviewer and the fixer read the same"):
            credentials.verify_credentials(loop, {"reviewer", "fixer"})
        loop["tokens"][BOT_REV] = str(self.pats["rev"])
        credentials.verify_credentials(loop, {"reviewer", "fixer"})
        # #1475 review: a file gone between its check and the comparison is refused, never
        # taken as distinct.
        real = Path.read_text

        def read_then_vanish(path, *args, **kwargs):
            text = real(path, *args, **kwargs)
            if path == self.pats["rev"]:
                path.unlink()
            return text
        with mock.patch.object(Path, "read_text", read_then_vanish), \
                self.assertRaisesRegex(config.ConfigError, "reviewer seat .* cannot be told apart"):
            credentials.verify_credentials(loop, {"reviewer", "fixer"})          # the reader's check
        with self.assertRaisesRegex(config.ConfigError, "cannot verify reviewer/fixer"):
            credentials._distinct_seat_files("widgets", self.pats["rev"], shared)  # and the seats'

    def test_the_exact_key_wins_in_the_cli_and_show(self):
        # A map that never went through load: the exact entry, never a variant, empty or not.
        from diaktoros import show
        from diaktoros.cli import common
        self.assertEqual(common._token_ref({"tokens": {tfs.FIX: "", tfs.FIX.upper(): "/p"}},
                                           tfs.FIX), "")
        loop = copy.deepcopy(self.loop)
        loop["tokens"] = {tfs.FIX: str(self.pats["fix"]), tfs.FIX.upper(): str(self.pats["rev"])}
        loop["apps"][BOT_REV.upper()] = {"app_id": 99, "key_file": str(self.read_key)}
        rows = {row["name"]: row for row in show.collect(loop, {})}
        self.assertEqual(rows["fixer_token_file"]["value"], str(self.pats["fix"]))
        self.assertEqual(rows["reviewer_app_id"]["value"], 2)


class Selftest(AppLoop):
    def identities(self, principal):
        report = selftest.Report(io.StringIO())

        def fake_principal(loop, login, fetch=None):
            if login == tfs.FIX:
                return {"login": tfs.FIX, "id": 3}
            return principal(login)
        with mock.patch.object(gh, "principal", side_effect=fake_principal), \
                mock.patch.object(gh, "fetch", side_effect=lambda loop, path, login=None:
                                  ({"full_name": "acme/widgets"}, "") if path.startswith("/repos/")
                                  else ({"login": login, "id": 3}, "")):
            ok = selftest.check_identities(report, self.loop)
        return ok, {r[1]: r for r in report.results}

    def test_a_token_key_in_another_case_is_mapped(self):
        # #1475 review: selftest wanted the exact key while every GitHub call took any case.
        self.loop["tokens"] = {tfs.FIX.upper(): self.loop["tokens"][tfs.FIX]}
        ids = {BOT_REV: 346013884, BOT_READ: 346012008}
        ok, rows = self.identities(lambda login: {"login": login, "id": ids[login],
                                                  "type": "Bot", "app_id": 9})
        self.assertNotIn("no token file mapped", rows["github:fixer"][3])
        self.assertEqual(rows["github:fixer"][2], selftest.PASS, rows["github:fixer"])

    def test_a_reader_with_no_login_is_told_both_credential_kinds(self):
        # #2183: the same reader fix every refusal prints, not a PAT-only one.
        self.loop["read_token"] = ""
        ok, rows = self.identities(lambda login: {"login": login, "id": 7, "type": "Bot", "app_id": 9})
        self.assertFalse(ok)
        self.assertEqual(rows["github:read"][4], credentials.reader_fix(self.loop, "LOGIN"))
        self.assertIn("--app LOGIN=APP_ID:/abs/key.pem", rows["github:read"][4])

    def test_an_unmapped_seat_is_told_the_settings_way(self):
        # #2240: not `set --token`, which refuses a seat's login.
        self.loop["tokens"].pop(tfs.FIX)
        ids = {BOT_REV: 346013884, BOT_READ: 346012008}
        ok, rows = self.identities(lambda login: {"login": login, "id": ids[login],
                                                  "type": "Bot", "app_id": 9})
        self.assertFalse(ok)
        self.assertEqual(rows["github:fixer"][4], credentials.seat_fix(self.loop, "fixer"))

    def test_app_identities_resolve_to_their_bots_without_user(self):
        ids = {BOT_REV: 346013884, BOT_READ: 346012008}
        ok, rows = self.identities(lambda login: {"login": login, "id": ids[login],
                                                  "type": "Bot", "app_id": 9})
        self.assertTrue(ok, rows)
        self.assertEqual(rows["github:reviewer"][2], selftest.PASS)
        self.assertIn("GitHub App 9", rows["github:reviewer"][3])

    def test_an_app_key_for_another_bot_or_no_installation_fails(self):
        ok, rows = self.identities(lambda login: {"login": "other[bot]", "id": 5})
        self.assertFalse(ok)
        self.assertIn("belongs to other[bot]", rows["github:reviewer"][3])

        def not_installed(login):
            raise github_app.AppError("App is not installed on acme/widgets")
        ok, rows = self.identities(not_installed)
        self.assertFalse(ok)
        self.assertIn("not installed", rows["github:reviewer"][3])
        # The usual shape: gh.principal answers None and the reason is in gh.last_error().
        with mock.patch.object(gh, "last_error", return_value="App X is not installed on acme/widgets"):
            ok, rows = self.identities(lambda login: None)
        self.assertFalse(ok)
        self.assertIn("App X is not installed", rows["github:reviewer"][3])


class Backup(AppLoop):
    def test_a_backup_records_app_keys_and_a_restore_names_a_missing_one(self):
        paths = backup.token_paths(self.loop)
        self.assertEqual(paths[BOT_REV], str(self.rev_key))
        self.assertEqual(paths[BOT_READ], str(self.read_key))
        manifest = {"tokens": {"widgets": paths}}
        self.assertEqual(backup.missing_tokens(manifest), [])
        self.rev_key.unlink()
        missing = backup.missing_tokens(manifest)
        self.assertEqual(len(missing), 1)
        self.assertIn(f"credential file for {BOT_REV}", missing[0])


if __name__ == "__main__":
    unittest.main()
