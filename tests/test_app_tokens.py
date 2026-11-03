"""#1780: an App's installation token is shared by the host's processes (``app_tokens``).

GitHub's ETags follow the token that asked, so a token minted per process made every
conditional read of #1653 a counted 200 for an App identity. A "new process" here is the same
process with its in-memory tokens dropped (``github_app._tokens``), except in the race test,
which runs real processes against one lock.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import app_tokens, gh, gh_cache, gh_calls, github_app, hostdirs  # noqa: E402

REPO, BOT, APP_ID = "acme/widgets", "acme-reader[bot]", 5416187
LATER = "2099-01-01T00:00:00Z"


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / "reader.pem"
        self.key.write_text("-----BEGIN PRIVATE KEY-----\nKEYBYTES\n-----END PRIVATE KEY-----\n")
        self.key.chmod(0o600)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "read_token": BOT,
                     "apps": {BOT: {"app_id": APP_ID, "key_file": str(self.key)}}}
        shutil.rmtree(app_tokens.directory(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, app_tokens.directory(), True)
        github_app.forget()
        self.addCleanup(github_app.forget)
        self.mints, self.expires = 0, LATER

        def as_app(loop, login, path, method="GET", body=None):
            if path == f"/repos/{REPO}/installation":
                return gh.Response({"id": 9}, "", 200, {})
            self.mints += 1
            return gh.Response({"token": f"ghs_{self.mints}_" + "x" * 380,
                                "expires_at": self.expires}, "", 201, {})
        for patch in (mock.patch.object(github_app, "_as_app", side_effect=as_app),
                      mock.patch.object(gh_cache, "_WRITES", True)):           # a writer
            patch.start()
            self.addCleanup(patch.stop)

    def new_process(self):
        github_app._tokens.clear()          # what a process starts with: nothing in memory


class Shared(Fixture):
    def test_a_second_process_reuses_the_first_ones_token(self):
        first = gh.token(self.loop, BOT)
        self.new_process()
        self.assertEqual(gh.token(self.loop, BOT), first)
        self.assertEqual(self.mints, 1)

    def test_a_shared_token_close_to_expiry_is_replaced_for_everyone(self):
        self.expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
        first = gh.token(self.loop, BOT)
        self.new_process()
        self.expires = LATER
        second = gh.token(self.loop, BOT)
        self.assertNotEqual(second, first)
        self.new_process()
        self.assertEqual(gh.token(self.loop, BOT), second)
        self.assertEqual(self.mints, 2)

    def test_the_file_is_private_and_holds_the_token_and_its_expiry_only(self):
        token = gh.token(self.loop, BOT)
        [file] = [f for f in app_tokens.directory().iterdir() if not f.name.startswith(".")]
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(app_tokens.directory().stat().st_mode), 0o700)
        stored = json.loads(file.read_text())
        self.assertEqual(set(stored), {"token", "expires"})
        self.assertEqual(stored["token"], token)
        self.assertNotIn("KEYBYTES", file.read_text())
        self.assertNotIn(BOT, file.name)                # named by a hash: no login on disk

    def test_a_process_that_writes_nothing_reads_a_shared_token_but_never_stores_one(self):
        first = gh.token(self.loop, BOT)
        self.new_process()
        with mock.patch.object(gh_cache, "_WRITES", False):        # doctor, the Desktop
            self.assertEqual(gh.token(self.loop, BOT), first)       # read
            github_app.forget()                                     # drops nothing on disk
            shutil.rmtree(app_tokens.directory())
            gh.token(self.loop, BOT)                                # mints, stores nothing
        self.assertFalse(app_tokens.directory().exists())

    def test_a_worker_shares_only_through_the_directory_the_host_made(self):
        with mock.patch.dict(os.environ, {hostdirs.WORKER_ENV: "1"}):
            gh.token(self.loop, BOT)
            self.assertFalse(app_tokens.directory().exists())       # never made by a worker
        app_tokens.prepare()                                        # the host, at spawn
        self.new_process()
        with mock.patch.dict(os.environ, {hostdirs.WORKER_ENV: "1"}):
            first = gh.token(self.loop, BOT)
            self.new_process()
            self.assertEqual(gh.token(self.loop, BOT), first)
        self.assertEqual(self.mints, 2)

    def test_a_refused_token_is_dropped_for_everyone_but_never_its_successor(self):
        first = gh.token(self.loop, BOT)
        github_app.refused(self.loop, BOT)                          # GitHub said 401
        self.new_process()
        second = gh.token(self.loop, BOT)
        self.assertNotEqual(second, first)
        # A process still holding the dead token is refused after another minted its successor:
        github_app._tokens[(BOT.casefold(), REPO, APP_ID)] = (first, time.time() + 3600)
        github_app.refused(self.loop, BOT)
        self.new_process()
        self.assertEqual(gh.token(self.loop, BOT), second)
        self.assertEqual(self.mints, 2)

    def test_a_backup_never_takes_a_token_even_from_a_state_dir_set_to_state_itself(self):
        from diaktoros import backup, config
        gh.token(self.loop, BOT)
        state = config.home() / "state"
        kept = state / "loop-state.json"
        kept.write_text("{}")
        self.addCleanup(kept.unlink)
        found = backup._regular_files(state)
        self.assertIn(kept, found)
        self.assertEqual([f for f in found if f.is_relative_to(app_tokens.directory())], [])

    def test_a_401_through_gh_drops_the_token(self):
        with mock.patch.object(gh, "API", "http://127.0.0.1:1"), \
                mock.patch("urllib.request.urlopen", side_effect=_http_error(401)), \
                mock.patch.dict(os.environ, {"DIAKTOROS_GH_STUB": "", "REVIEW_LOOP_GH_STUB": ""}):
            first = gh.token(self.loop, BOT)
            self.assertEqual(gh.request(self.loop, f"/repos/{REPO}").status, 401)
        self.new_process()
        self.assertNotEqual(gh.token(self.loop, BOT), first)


def _http_error(code):
    import email.message
    import io
    import urllib.error

    def raise_(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "no", email.message.Message(),
                                     io.BytesIO(b""))
    return raise_


class ConditionalReads(Fixture):
    """End to end through ``gh.request``, on an API whose ETags follow the token, as GitHub's."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(gh_cache.directory(), ignore_errors=True)
        shutil.rmtree(gh_calls.directory(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, gh_cache.directory(), True)
        self.addCleanup(shutil.rmtree, gh_calls.directory(), True)
        gh_calls._counts.clear()
        self.addCleanup(gh_calls._counts.clear)

        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                tag = '"%s"' % hashlib.sha256(self.headers["Authorization"].encode()).hexdigest()
                if self.headers.get("If-None-Match") == tag:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.end_headers()
                    return
                body = b'{"id": 1}'
                self.send_response(200)
                self.send_header("ETag", tag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for patch in (mock.patch.object(gh, "API", f"http://127.0.0.1:{server.server_port}"),
                      mock.patch.dict(os.environ, {"DIAKTOROS_GH_STUB": "",
                                                   "REVIEW_LOOP_GH_STUB": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def test_an_unchanged_read_from_a_second_process_is_free(self):
        path = f"/repos/{REPO}/pulls/7"
        self.assertEqual(gh.request(self.loop, path).status, 200)
        self.new_process()
        self.assertEqual(gh.request(self.loop, path).data, {"id": 1})
        gh_calls.flush()
        counted = gh_calls.summary(time.time() - 3600)[BOT]
        self.assertEqual((counted["spent"], counted["free"]), (1, 1))


class Race(unittest.TestCase):
    def test_processes_racing_for_a_token_mint_one(self):
        # Real processes against one lock: each takes the lock, finds no token, would mint.
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        home, log = Path(temp.name), Path(temp.name) / "mints"
        script = f"""
import os, sys, time
sys.path.insert(0, {str(ROOT)!r})
from unittest import mock
from diaktoros import gh, gh_cache, github_app
gh_cache.enable_writes()
def as_app(loop, login, path, method="GET", body=None):
    if path.endswith("/installation"):
        return gh.Response({{"id": 9}}, "", 200, {{}})
    with open({str(log)!r}, "a") as f:
        f.write("mint\\n")
    time.sleep(0.3)                                   # a slow mint: the others wait for it
    return gh.Response({{"token": "ghs_" + str(os.getpid()), "expires_at": {LATER!r}}}, "", 201, {{}})
loop = {{"repo": {REPO!r}, "read_token": {BOT!r}, "apps": {{{BOT!r}: {{"app_id": {APP_ID}, "key_file": "/k"}}}}}}
with mock.patch.object(github_app, "_as_app", side_effect=as_app):
    print(github_app.installation_token(loop, {BOT!r}))
"""
        env = {**os.environ, "HERMES_HOME": str(home), "HOME": str(home)}
        procs = [subprocess.Popen([sys.executable, "-c", script], env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL) for _ in range(4)]
        outs = [p.communicate(timeout=60) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 4, [e for _o, e in outs])
        self.assertEqual(len({o.strip() for o, _e in outs}), 1)      # one token for all four
        self.assertEqual(log.read_text().count("mint"), 1)


if __name__ == "__main__":
    unittest.main()
