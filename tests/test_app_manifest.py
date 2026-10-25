"""#1163 M4: `hermes dk create-apps` makes a loop's GitHub Apps from manifests. The browser round
trip runs against the real 127.0.0.1 server; only GitHub's code conversion is faked."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import html
import json
import pathlib
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import app_manifest as am  # noqa: E402

HOST = "https://gw.example"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nNOT-A-REAL-KEY\n-----END RSA PRIVATE KEY-----\n"


class Manifests(unittest.TestCase):
    def test_each_role_gets_the_proven_permissions_and_only_the_reader_a_webhook(self):
        reader = am.manifest("widgets-dk", "reader", HOST, "http://127.0.0.1:1/callback", "u")
        self.assertEqual(reader["name"], "widgets-dk-reader")
        self.assertEqual(reader["hook_attributes"],
                         {"url": f"{HOST}/webhooks/dk-app-widgets-dk-reader", "active": True})
        self.assertEqual(reader["default_events"], ["pull_request", "pull_request_review", "issues"])
        self.assertEqual(reader["default_permissions"]["repository_hooks"], "write")
        self.assertFalse(reader["public"])
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                body = am.manifest("widgets-dk", role, HOST, "r", "u")
                self.assertFalse(body["hook_attributes"]["active"])
                self.assertEqual(body["default_events"], [])
                self.assertEqual(body["default_permissions"]["metadata"], "read")
        self.assertEqual(am.manifest("w", "reviewer", HOST, "r", "u")["default_permissions"]["checks"],
                         "write")
        with self.assertRaisesRegex(am.WizardError, "longer than GitHub allows"):
            am.app_name("x" * 30, "adjudicator")

    def test_the_page_posts_the_manifest_escaped(self):
        body = am.manifest("a\"<b>", "fixer", HOST, "r", "u")
        page = am.form_page("https://github.com/settings/apps/new?state=s", body).decode()
        self.assertNotIn("<b>", page.split("<p>")[0])           # the manifest value is escaped
        value = re.search(r'name="manifest" value="([^"]*)"', page).group(1)
        self.assertEqual(json.loads(html.unescape(value)), body)

    def test_codes_and_answers_are_checked(self):
        with self.assertRaisesRegex(am.WizardError, "not a manifest code"):
            am.convert("../../etc", post=lambda url: {})
        with self.assertRaisesRegex(am.WizardError, "did not carry"):
            am.convert("abc", post=lambda url: {"id": 1})
        seen = []
        app = am.convert("abc", post=lambda url: seen.append(url) or
                         {"id": 7, "slug": "w-dk-reader", "pem": PEM, "webhook_secret": "s"})
        self.assertEqual(seen, ["https://api.github.com/app-manifests/abc/conversions"])
        self.assertEqual(app["id"], 7)

    def test_a_pasted_redirect_url_must_carry_this_apps_state(self):
        self.assertEqual(am.code_from("https://x.example/cb?code=abc&state=S1", "S1"), "abc")
        self.assertEqual(am.code_from("  abc  ", "S1"), "abc")
        with self.assertRaisesRegex(am.WizardError, "state is not this App's"):
            am.code_from("https://x.example/cb?code=abc&state=OTHER", "S1")

    def test_next_steps_for_a_loop_and_for_a_new_one(self):
        made = {"reader": {"slug": "w-dk-reader", "id": 1, "login": "w-dk-reader[bot]",
                           "key_file": "/k/r.pem"},
                "reviewer": {"slug": "w-dk-reviewer", "id": 2, "login": "w-dk-reviewer[bot]",
                             "key_file": "/k/v.pem"}}
        steps = am.next_steps(made, "coe0718", False, "widgets")
        self.assertIn("https://github.com/apps/w-dk-reader/installations/new", steps[0])
        self.assertIn('hermes dk identity --loop widgets --seat read --app "w-dk-reader[bot]=1:/k/r.pem"',
                      steps)
        self.assertTrue(any("--ingress app-webhook" in s for s in steps))
        self.assertTrue(any(s.startswith("hermes dk protect") for s in steps))
        fresh = am.next_steps(made, "coe0718", False, None)
        self.assertTrue(fresh[-1].startswith("hermes dk setup --repo OWNER/REPO --app"))


class BrowserRound(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.key_dir = pathlib.Path(tmp.name) / "apps"
        self.log = []

    def browser(self, *, host_header=None, wrong_state=False):
        """A stand-in browser: load the page, read the manifest's state, follow GitHub back."""
        def visit(start):
            def go():
                port = urllib.parse.urlsplit(start).port
                headers = {"Host": host_header} if host_header else {}
                try:
                    page = urllib.request.urlopen(urllib.request.Request(start, headers=headers),
                                                  timeout=5).read().decode()
                except urllib.error.HTTPError as exc:
                    exc.close()
                    self.log.append(("page", exc.code))
                    return
                action = html.unescape(re.search(r'action="([^"]*)"', page).group(1))
                state = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)["state"][0]
                manifest = json.loads(html.unescape(
                    re.search(r'name="manifest" value="([^"]*)"', page).group(1)))
                self.log.append(("manifest", manifest["name"], manifest["redirect_url"]))
                back = (f"http://127.0.0.1:{port}/callback?code=c-{manifest['name']}"
                        f"&state={'nope' if wrong_state else state}")
                try:
                    urllib.request.urlopen(back, timeout=5).read()
                except urllib.error.HTTPError as exc:
                    exc.close()
                    self.log.append(("callback", exc.code))
            threading.Thread(target=go, daemon=True).start()
        return visit

    def post(self, url):
        code = url.split("/app-manifests/")[1].split("/")[0]
        name = code.removeprefix("c-")
        return {"id": len(name), "slug": name, "pem": PEM, "webhook_secret": "whsec"}

    def create(self, roles=("reader", "reviewer"), **kw):
        out = []
        made = am.create(owner="coe0718", org=False, prefix="w-dk", roles=roles, host=HOST,
                         key_dir=self.key_dir, emit=out.append, post=self.post,
                         open_url=kw.pop("open_url", self.browser()), wait_s=kw.pop("wait_s", 10),
                         **kw)
        return made, "\n".join(out)

    def test_each_app_is_made_its_key_written_private_and_never_printed(self):
        made, out = self.create()
        self.assertEqual(sorted(made), ["reader", "reviewer"])
        self.assertEqual(made["reader"]["login"], "w-dk-reader[bot]")
        key = pathlib.Path(made["reader"]["key_file"])
        self.assertEqual(key.read_text(), PEM)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("NOT-A-REAL-KEY", out)
        session = (self.key_dir / am.SESSION).read_text()
        self.assertNotIn("NOT-A-REAL-KEY", session)
        self.assertNotIn("whsec", session)
        redirect = [entry[2] for entry in self.log if entry[0] == "manifest"][0]
        self.assertRegex(redirect, r"^http://127\.0\.0\.1:\d+/callback$")

    def test_a_rerun_resumes_and_a_key_is_never_overwritten(self):
        self.create(roles=("reader",))
        made, out = self.create(roles=("reader", "fixer"))
        self.assertIn("reader: w-dk-reader (made earlier", out)
        self.assertEqual(sorted(made), ["fixer", "reader"])
        with self.assertRaises(FileExistsError):
            am.write_key(self.key_dir, "w-dk-reader", "other")

    def test_a_request_to_another_host_name_is_refused(self):
        with self.assertRaisesRegex(am.WizardError, "no answer from GitHub"):
            self.create(roles=("reader",), open_url=self.browser(host_header="evil.example"),
                        wait_s=1)
        self.assertIn(("page", 403), self.log)

    def test_a_callback_with_another_state_is_refused(self):
        with self.assertRaisesRegex(am.WizardError, "no answer from GitHub"):
            self.create(roles=("reader",), open_url=self.browser(wrong_state=True), wait_s=1)
        self.assertIn(("callback", 403), self.log)
        self.assertFalse((self.key_dir / "w-dk-reader.pem").exists())

    def test_a_second_callback_is_refused_and_the_first_code_kept(self):
        def visit(start):
            def go():
                page = urllib.request.urlopen(start, timeout=5).read().decode()
                action = html.unescape(re.search(r'action="([^"]*)"', page).group(1))
                state = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)["state"][0]
                back = f"{start}callback?state={state}&code="
                urllib.request.urlopen(back + "c-w-dk-reader", timeout=5).read()
                try:
                    urllib.request.urlopen(back + "c-other", timeout=5).read()
                except urllib.error.HTTPError as exc:
                    exc.close()
                    self.log.append(("again", exc.code))
                except OSError:                    # the server already shut down: also refused
                    self.log.append(("again", "closed"))
            threading.Thread(target=go, daemon=True).start()
        made, _ = self.create(roles=("reader",), open_url=visit)
        self.assertEqual(made["reader"]["slug"], "w-dk-reader")

    def test_without_a_browser_the_page_is_a_private_file_and_the_pasted_url_is_checked(self):
        pages, out = [], []

        def ask(_prompt):
            page = sorted(self.key_dir.glob("*-manifest.html"))[0]
            pages.append((page, page.stat().st_mode & 0o777))
            text = page.read_text()
            action = html.unescape(re.search(r'action="([^"]*)"', text).group(1))
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(action).query)["state"][0]
            manifest = json.loads(html.unescape(
                re.search(r'name="manifest" value="([^"]*)"', text).group(1)))
            self.assertEqual(manifest["redirect_url"], "https://github.com/coe0718/diaktoros")
            return f"{manifest['redirect_url']}?code=c-{manifest['name']}&state={state}"

        made = am.create(owner="coe0718", org=False, prefix="w-dk", roles=("fixer",), host=HOST,
                         key_dir=self.key_dir, browser=False, emit=out.append, ask=ask,
                         post=self.post)
        self.assertEqual(made["fixer"]["slug"], "w-dk-fixer")
        self.assertEqual(pages[0][1], 0o600)
        self.assertFalse(pages[0][0].exists())            # removed once used
        with self.assertRaisesRegex(am.WizardError, "state is not this App's"):
            am.create(owner="coe0718", org=False, prefix="w-dk", roles=("reader",), host=HOST,
                      key_dir=self.key_dir, browser=False, emit=out.append, post=self.post,
                      ask=lambda _p: "https://github.com/x?code=c-w-dk-reader&state=forged")
        self.assertFalse((self.key_dir / "w-dk-reader.pem").exists())


class CreateAppsCommand(unittest.TestCase):
    def run_cli(self, *argv):
        import contextlib
        import io
        from diaktoros.cli import checks as cli
        parser = __import__("argparse").ArgumentParser()
        sub = parser.add_subparsers()
        cli.add_create_apps(sub)
        args = parser.parse_args(["create-apps", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_refusals_come_before_any_browser_or_network(self):
        rc, out = self.run_cli("--owner", "coe0718", "--prefix", "w-dk")
        self.assertEqual(rc, 2)
        self.assertIn("--host", out)
        rc, out = self.run_cli("--owner", "coe0718", "--host", HOST)
        self.assertEqual(rc, 2)
        self.assertIn("--prefix", out)
        rc, out = self.run_cli("--owner", "coe0718", "--host", HOST, "--prefix", "x" * 30)
        self.assertEqual(rc, 1)
        self.assertIn("longer than GitHub allows", out)
        rc, out = self.run_cli("--owner", "coe0718", "--host", HOST, "--prefix", "w",
                               "--roles", "reader,boss", "--key-dir", tempfile.mkdtemp())
        self.assertEqual(rc, 1)
        self.assertIn("unknown role 'boss'", out)

    def test_no_browser_opens_before_every_role_is_known(self):
        opened = []
        with self.assertRaisesRegex(am.WizardError, "unknown role 'boss'"):
            am.create(owner="o", org=False, prefix="w", roles=("reader", "boss"), host=HOST,
                      key_dir=pathlib.Path(tempfile.mkdtemp()), open_url=opened.append,
                      emit=lambda _l: None, wait_s=0)
        self.assertEqual(opened, [])
        prefix = "p" * 23                   # the reader's name fits; the adjudicator's does not
        with self.assertRaisesRegex(am.WizardError, "longer than GitHub allows"):
            am.create(owner="o", org=False, prefix=prefix, roles=("reader", "adjudicator"),
                      host=HOST, key_dir=pathlib.Path(tempfile.mkdtemp()),
                      open_url=opened.append, emit=lambda _l: None, wait_s=0)
        self.assertEqual(opened, [])

    def test_the_callback_takes_one_code_only(self):
        round_ = am._Round("https://github.com/settings/apps/new?state=S", lambda r: {}, "S")
        thread = threading.Thread(target=round_.server.serve_forever, daemon=True)
        thread.start()
        try:
            back = f"http://127.0.0.1:{round_.port}/callback?state=S&code="
            urllib.request.urlopen(back + "first", timeout=5).read()
            with self.assertRaises(urllib.error.HTTPError) as again:
                urllib.request.urlopen(back + "second", timeout=5).read()
            again.exception.close()
        finally:
            round_.server.shutdown()
            round_.server.server_close()
        self.assertEqual(again.exception.code, 409)
        self.assertEqual(round_.code, "first")


if __name__ == "__main__":
    unittest.main()
