"""GitHub App identities at runtime (#1163 M1b): a seat or the reader as an App's bot.

An App identity is configured in ``apps`` with its numeric id and private key. The host signs an
App JWT (RS256) with the key, mints an installation token scoped to the loop's repository, and
uses it wherever a PAT is used (``gh.token``). Its principal is the App's bot user, because an
installation token cannot call ``GET /user``. Pushes feed git a minted token from a private file.
PAT identities are untouched: ``token_path`` stays PAT-only, so nothing reads a key as a token.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, config, gh, github_app, safe_push  # noqa: E402
from diaktoros.config import credentials  # noqa: E402

OPENSSL = shutil.which("openssl", path="/usr/bin:/bin:/usr/local/bin")
REPO = "acme/widgets"
HEAD = "a" * 40
BOT = "acme-reviewer[bot]"
APP_ID = 5416187


def b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        self.key = self.root / "reviewer.pem"
        if OPENSSL:
            subprocess.run([OPENSSL, "genrsa", "-out", str(self.key), "2048"], check=True,
                           capture_output=True)
        else:
            self.key.write_text("not a key")
        self.key.chmod(0o600)
        tokens = {}
        for login in ("read", "fix"):
            path = self.root / f"{login}.pat"
            path.write_text("DUMMY_PAT_" + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "fixers": ["fix"],
                     "reviewers": [BOT], "tokens": tokens, "read_token": "read",
                     "reviewer_seat": BOT, "apps": {BOT: {"app_id": APP_ID,
                                                          "key_file": str(self.key)}},
                     "seats": {"reviewer": {"login": BOT}, "fixer": {"login": "fix"}}}
        github_app.forget()
        self.addCleanup(github_app.forget)
        self.app_calls = []
        self.expires = "2099-01-01T00:00:00Z"

        def as_app(loop, login, path, method="GET", body=None):
            self.app_calls.append((method, path, body))
            if path == f"/repos/{REPO}/installation":
                return gh.Response({"id": 173885357}, "", 200, {})
            if path.endswith("/access_tokens"):
                return gh.Response({"token": f"ghs_{len(self.app_calls)}" + "x" * 386,
                                    "expires_at": self.expires}, "", 201, {})
            if path == "/app":
                return gh.Response({"id": APP_ID, "slug": "acme-reviewer"}, "", 200, {})
            return gh.Response(None, "HTTP 404", 404, {})
        patcher = mock.patch.object(github_app, "_as_app", side_effect=as_app)
        patcher.start()
        self.addCleanup(patcher.stop)


class Config(unittest.TestCase):
    def test_an_app_identity_is_its_bot_login_with_an_id_and_a_key(self):
        got = config.check_apps({BOT: {"app_id": APP_ID, "key_file": "/k/r.pem"}}, {}, "w")
        self.assertEqual(got, {BOT: {"app_id": APP_ID, "key_file": "/k/r.pem"}})
        self.assertEqual(config.check_apps(None, {}, "w"), {})

    def test_malformed_app_identities_are_refused(self):
        good = {"app_id": APP_ID, "key_file": "/k/r.pem"}
        for apps, why in (({"acme-reviewer": good}, "bot login"),
                          ({BOT: {**good, "app_id": "5"}}, "numeric id"),
                          ({BOT: {**good, "app_id": True}}, "numeric id"),
                          ({BOT: {**good, "app_id": 0}}, "numeric id"),
                          ({BOT: {"app_id": APP_ID}}, "key_file"),
                          ({BOT: {**good, "key_file": "r.pem"}}, "key_file"),
                          ({BOT: {**good, "secret": "x"}}, "must be"),
                          ({BOT: "r.pem"}, "must be"),
                          ([BOT], "object")):
            with self.subTest(apps=apps), self.assertRaisesRegex(config.ConfigError, why):
                config.check_apps(apps, {}, "w")

    def test_one_identity_uses_one_credential(self):
        with self.assertRaisesRegex(config.ConfigError, "both 'tokens' and 'apps'"):
            config.check_apps({BOT: {"app_id": APP_ID, "key_file": "/k/r.pem"}},
                              {"Acme-Reviewer[bot]": "/k/r.pat"}, "w")

    def test_the_credentials_map_holds_pat_files_and_app_keys(self):
        loop = {"tokens": {"read": "/k/read.pat"},
                "apps": {BOT: {"app_id": APP_ID, "key_file": "/k/r.pem"}}}
        self.assertEqual(config.credentials_map(loop), {"read": "/k/read.pat", BOT: "/k/r.pem"})


class Normalize(unittest.TestCase):
    """The loop file loads with App seats: the triage login (the reviewer seat's, by default) and
    the adjudicator comment identity count an App key as their credential."""

    def raw(self, **extra):
        return {"repo": REPO, "fixers": ["fix"], "reviewers": [BOT], "read_token": "reader",
                "tokens": {"reader": "/k/reader.pat", "fix": "/k/fix.pat"},
                "apps": {BOT: {"app_id": 1, "key_file": "/k/rev.pem"},
                         "acme-arbiter[bot]": {"app_id": 2, "key_file": "/k/arb.pem"}},
                "seats": {"reviewer": {"profile": "r", "login": BOT, "route": "w-review"},
                          "fixer": {"profile": "f", "login": "fix", "route": "w-fix"},
                          "adjudicator": {"login": "acme-arbiter[bot]"}},
                "adjudicator": {"route": "w-breach"},
                "triage": {"route": "w-triage", "profile": "r", "authors": ["fix"],
                           "labels": ["bug"]}, **extra}

    def test_app_seats_load(self):
        loop = config.normalize(self.raw())
        self.assertEqual(config.triage_login(loop), BOT)
        self.assertEqual(config.adjudicator_login(loop), "acme-arbiter[bot]")
        self.assertEqual(loop["apps"][BOT], {"app_id": 1, "key_file": "/k/rev.pem"})

    def test_without_their_app_entry_they_are_refused(self):
        raw = self.raw()
        del raw["apps"]["acme-arbiter[bot]"]
        with self.assertRaisesRegex(config.ConfigError, "no entry in 'tokens' or 'apps'"):
            config.normalize(raw)
        raw = self.raw()
        del raw["apps"][BOT]
        with self.assertRaisesRegex(config.ConfigError, "no token mapped for the triage login"):
            config.normalize(raw)

    def test_an_app_key_shared_with_another_identity_is_refused(self):
        raw = self.raw()
        raw["apps"]["acme-arbiter[bot]"]["key_file"] = "/k/fix.pat"
        with self.assertRaisesRegex(config.ConfigError, "read the same token file"):
            config.normalize(raw)


class Credentials(Base):
    def test_verify_credentials_accepts_an_app_seat_and_checks_its_key(self):
        credentials.verify_credentials(self.loop, {"read", "reviewer", "fixer"})
        self.key.chmod(0o644)
        with self.assertRaisesRegex(config.ConfigError, "App key for .*group/other"):
            credentials.verify_credentials(self.loop, {"reviewer"})

    def test_a_seat_with_neither_credential_is_refused(self):
        self.loop["apps"] = {}
        with self.assertRaisesRegex(config.ConfigError, "no token mapped for the reviewer"):
            credentials.verify_credentials(self.loop, {"reviewer"})


@unittest.skipUnless(OPENSSL, "openssl is not installed here")
class Signing(Base):
    def test_the_jwt_is_rs256_signed_by_the_apps_key_and_names_the_app(self):
        with mock.patch.object(github_app, "_runtime_python", return_value=None):
            token = github_app.jwt(self.loop, BOT, now=1_800_000_000)
        head, body, sig = token.split(".")
        self.assertEqual(json.loads(b64decode(head)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64decode(body)),
                         {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + github_app.JWT_TTL_S,
                          "iss": str(APP_ID)})
        public = self.root / "pub.pem"
        subprocess.run([OPENSSL, "rsa", "-in", str(self.key), "-pubout", "-out", str(public)],
                       check=True, capture_output=True)
        signature = self.root / "sig.bin"
        signature.write_bytes(b64decode(sig))
        verified = subprocess.run([OPENSSL, "dgst", "-sha256", "-verify", str(public),
                                   "-signature", str(signature)],
                                  input=f"{head}.{body}".encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_the_runtime_pythons_cryptography_is_preferred_when_it_has_it(self):
        with mock.patch.object(github_app, "_runtime_python", return_value=Path(sys.executable)), \
                mock.patch.object(subprocess, "run", wraps=subprocess.run) as run:
            kind, tool = github_app.signer()
        has = subprocess.run([sys.executable, "-I", "-c", "import cryptography"],
                             capture_output=True).returncode == 0
        self.assertEqual(kind, "cryptography" if has else "openssl")
        if has:
            self.assertEqual(tool, Path(sys.executable))
            with mock.patch.object(github_app, "_runtime_python", return_value=Path(sys.executable)):
                github_app.jwt(self.loop, BOT)
        self.assertTrue(run.called)

    def test_a_bad_key_fails_without_echoing_it(self):
        self.key.write_text("-----BEGIN RSA PRIVATE KEY-----\nSECRETSTUFF\n")
        with mock.patch.object(github_app, "_runtime_python", return_value=None), \
                self.assertRaises(github_app.AppError) as caught:
            github_app.jwt(self.loop, BOT)
        self.assertNotIn("SECRETSTUFF", str(caught.exception))
        self.assertIn("signing failed", str(caught.exception))

    def test_no_signer_says_so(self):
        with mock.patch.object(github_app, "signer", return_value=("", None)), \
                self.assertRaisesRegex(github_app.AppError, "no App signer"):
            github_app.jwt(self.loop, BOT)


class Tokens(Base):
    def test_a_token_is_minted_for_the_loops_repository_and_reused(self):
        first = gh.token(self.loop, BOT)
        self.assertTrue(first.startswith("ghs_"))
        self.assertEqual(self.app_calls[0], ("GET", f"/repos/{REPO}/installation", None))
        self.assertEqual(self.app_calls[1], ("POST", "/app/installations/173885357/access_tokens",
                                             {"repositories": ["widgets"]}))
        self.assertEqual(gh.token(self.loop, BOT), first)            # reused: no new calls
        self.assertEqual(len(self.app_calls), 2)

    def test_a_token_close_to_expiry_is_replaced(self):
        self.expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
        first = gh.token(self.loop, BOT)
        self.assertNotEqual(gh.token(self.loop, BOT), first)
        self.assertEqual(len(self.app_calls), 4)

    def test_an_app_not_installed_on_the_repository_is_refused(self):
        def missing(loop, login, path, method="GET", body=None):
            return gh.Response(None, "HTTP 404 Not Found", 404, {})
        with mock.patch.object(github_app, "_as_app", side_effect=missing), \
                self.assertRaisesRegex(gh.GitHubError, "not installed on acme/widgets"):
            gh.token(self.loop, BOT)

    def test_pat_identities_are_unchanged(self):
        self.assertEqual(gh.token(self.loop, "read"), "DUMMY_PAT_read")
        self.assertEqual(gh.token(self.loop), "DUMMY_PAT_read")
        self.assertEqual(self.app_calls, [])

    def test_the_key_is_a_credential_path_but_never_a_token_path(self):
        self.assertIsNone(gh.token_path(self.loop, BOT))
        self.assertEqual(gh.credential_path(self.loop, BOT), self.key)
        self.assertEqual(gh.credential_path(self.loop, "fix"), Path(self.loop["tokens"]["fix"]))


class Principal(Base):
    def users(self, answer):
        real = gh.api

        def api(loop, path, method="GET", body=None, login=None):
            if path == f"/users/{BOT}":
                return answer
            return real(loop, path, method=method, body=body, login=login)
        return mock.patch.object(gh, "api", side_effect=api)

    def test_an_apps_principal_is_its_bot_user(self):
        with self.users({"login": BOT, "id": 346013884, "type": "Bot"}):
            got = gh.principal(self.loop, BOT)
        self.assertEqual((got["login"], got["id"], got["type"], got["app_id"]),
                         (BOT, 346013884, "Bot", APP_ID))
        self.assertNotIn(("GET", "/user", None), self.app_calls)

    def test_a_key_for_another_app_or_a_user_answer_is_not_this_principal(self):
        def other(loop, login, path, method="GET", body=None):
            return gh.Response({"id": 999, "slug": "other-app"}, "", 200, {})
        with mock.patch.object(github_app, "_as_app", side_effect=other):
            self.assertIsNone(gh.principal(self.loop, BOT))
        github_app.forget()
        with self.users({"login": BOT, "id": 5, "type": "User"}):
            self.assertIsNone(gh.principal(self.loop, BOT))

    def test_the_watchdog_probe_reads_the_repository_for_an_app_reader(self):
        self.loop["apps"]["acme-reader[bot]"] = {"app_id": 1, "key_file": str(self.key)}
        self.loop["read_token"] = "acme-reader[bot]"
        del self.loop["tokens"]["read"]
        with mock.patch.object(gh, "request", return_value=gh.Response({}, "", 200, {})) as req:
            gh.auth_probe(self.loop)
        req.assert_called_once_with(self.loop, f"/repos/{REPO}")
        self.loop["read_token"] = "fix"
        with mock.patch.object(gh, "request", return_value=gh.Response({}, "", 200, {})) as req:
            gh.auth_probe(self.loop)
        req.assert_called_once_with(self.loop, "/user")


class Broker(Base):
    """The real authorizer, with the reviewer seat an App and the reader and fixer PATs."""

    def setUp(self):
        super().setUp()
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": REPO}},
                   "base": {"ref": "main", "repo": {"full_name": REPO}}}

        def api(loop, path, method="GET", body=None, login=None):
            if path == "/user":
                return {"login": login, "id": {"read": 1, "fix": 3}[login]}
            if path == f"/users/{BOT}":
                return {"login": BOT, "id": 346013884, "type": "Bot"}
            return self.pr
        patcher = mock.patch.object(gh, "api", side_effect=api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def authorize(self):
        return broker.authorize(self.loop, repo=REPO, number=7, head=HEAD, role="reviewer",
                                branch="fix-7", operation="review")

    def test_an_app_reviewer_is_authorized_as_its_bot(self):
        self.assertEqual(self.authorize(), BOT)

    def test_an_app_key_for_another_app_is_refused(self):
        def other(loop, login, path, method="GET", body=None):
            if path == "/app":
                return gh.Response({"id": 999, "slug": "other-app"}, "", 200, {})
            return gh.Response({"id": 1}, "", 200, {}) if path.endswith("installation") else \
                gh.Response({"token": "ghs_x", "expires_at": self.expires}, "", 201, {})
        with mock.patch.object(github_app, "_as_app", side_effect=other), \
                self.assertRaisesRegex(broker.BrokerDenied, "principal cannot be verified"):
            self.authorize()

    def test_an_app_key_shared_with_another_identity_is_refused(self):
        self.loop["tokens"]["fix"] = str(self.key)
        with self.assertRaisesRegex(broker.BrokerDenied, "distinct token files"):
            self.authorize()


@unittest.skipUnless(Path("/usr/bin/git").exists(), "git is not installed here")
class Push(Base):
    remote = property(lambda self: str(self.root / "remote.git"))     # a local remote

    def test_an_app_fixer_pushes_with_a_minted_token_in_a_private_file(self):
        identity = {"name": BOT, "email": f"1+{BOT}@users.noreply.github.com"}
        with safe_push._isolated(self.loop, BOT, identity, self.remote) as (git, _url):
            token_file = Path(git.env["DIAKTOROS_TOKEN_FILE"])
            self.assertEqual(token_file.parent, git.root)
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
            self.assertTrue(token_file.read_text().startswith("ghs_"))
            self.assertNotEqual(token_file, self.key)
        self.assertFalse(token_file.exists())

    def test_a_pat_fixer_still_reads_its_token_file(self):
        identity = {"name": "fix", "email": "3+fix@users.noreply.github.com"}
        with safe_push._isolated(self.loop, "fix", identity, self.remote) as (git, _url):
            self.assertEqual(git.env["DIAKTOROS_TOKEN_FILE"], self.loop["tokens"]["fix"])
        self.assertEqual(self.app_calls, [])

    def test_an_unusable_app_credential_refuses_the_push(self):
        with mock.patch.object(gh, "token", side_effect=github_app.AppError("not installed")), \
                self.assertRaisesRegex(broker.BrokerDenied, "fixer credential unavailable"):
            with safe_push._isolated(self.loop, BOT, {"name": BOT, "email": "e"}, self.remote):
                pass


if __name__ == "__main__":
    unittest.main()
