"""#1780: an App's installation token is shared by the host's processes (``app_tokens``).

GitHub's ETags follow the token that asked, so a token minted per process made every
conditional read of #1653 a counted 200 for an App identity. A "new process" here is the same
process with its in-memory tokens dropped (``github_app._tokens``), except in the race test,
which runs real processes against one lock.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
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
OTHER = "acme-other[bot]"
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
                     "apps": {BOT: {"app_id": APP_ID, "key_file": str(self.key)},
                              OTHER: {"app_id": 2, "key_file": str(self.key)}}}
        shutil.rmtree(app_tokens.directory(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, app_tokens.directory(), True)
        github_app.forget()
        self.addCleanup(github_app.forget)
        self.mints, self.expires, self.slow = 0, LATER, {}

        def as_app(loop, login, path, method="GET", body=None):
            if path == f"/repos/{REPO}/installation":
                return gh.Response({"id": 9}, "", 200, {})
            if login in self.slow:                    # a mint that hangs until released
                self.slow[login].wait(10)
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
        github_app.refused(self.loop, BOT, first)                   # GitHub said 401
        self.new_process()
        second = gh.token(self.loop, BOT)
        self.assertNotEqual(second, first)
        # Another process, still holding the dead token, is refused after the successor exists:
        github_app.refused(self.loop, BOT, first)
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
        with _patches(_http_error(401)):
            first = gh.token(self.loop, BOT)
            self.assertEqual(gh.request(self.loop, f"/repos/{REPO}").status, 401)
        self.new_process()
        self.assertNotEqual(gh.token(self.loop, BOT), first)

    def test_a_process_that_writes_nothing_stops_using_a_token_github_refused(self):
        # Review of #1781, F2: it cannot drop the shared file, so it reloaded the dead token.
        dead = gh.token(self.loop, BOT)
        self.new_process()
        sent = []
        with mock.patch.object(gh_cache, "_WRITES", False), _patches(_http_error(401, sent)):
            self.assertEqual(gh.request(self.loop, f"/repos/{REPO}").status, 401)
            self.assertEqual(sent, [dead])
            self.assertNotEqual(gh.token(self.loop, BOT), dead)
        self.assertEqual(app_tokens.load(KEY)[0], dead)             # it wrote nothing

    def test_a_401_while_staging_a_turn_drops_the_token_and_the_retry_uses_a_new_one(self):
        # Review of #1781, F3: trusted_fetch sends the token on a request of its own.
        from diaktoros import trusted_fetch
        sent = []

        def answer(req, timeout=None):                # 401 to the first token, 404 after
            _http_error(404 if sent else 401, sent)(req, timeout)
        with _patches(answer):
            for _ in range(2):
                with self.assertRaises(trusted_fetch.FetchDenied):
                    trusted_fetch._json(self.loop, f"/repos/{REPO}/pulls/7", BOT)
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])
        self.new_process()
        self.assertEqual(gh.token(self.loop, BOT), sent[1])          # the replacement is shared

    def test_a_401_to_a_log_read_drops_the_token_but_a_storage_hosts_does_not(self):
        # Review of #1781, F7: gh.read_text sends the token on a request of its own. GitHub's
        # 401 drops it; a 401 from the storage host it redirected to is not about the token.
        sent = []
        path = f"/repos/{REPO}/actions/jobs/5/logs"

        def storage(req, timeout=None):
            req.answered = {"x-ratelimit-used": "1"}  # GitHub answered with its redirect
            _http_error(401, sent)(req, timeout)
        with _patches(storage):
            self.assertIsNone(gh.read_text(self.loop, path))
            self.assertIsNone(gh.read_text(self.loop, path))
        self.assertEqual(sent[0], sent[1])             # kept: the storage host's refusal
        with _patches(_http_error(401, sent)):
            self.assertIsNone(gh.read_text(self.loop, path))
            self.assertIsNone(gh.read_text(self.loop, path))
        self.assertEqual(sent[2], sent[1])
        self.assertNotEqual(sent[3], sent[2])          # GitHub's 401: the retry has a new one

    def test_a_401_to_a_tarball_read_drops_the_token_and_the_retry_uses_a_new_one(self):
        from diaktoros import trusted_fetch
        sent = []
        with _patches(_http_error(401, sent)):
            for _ in range(2):
                with self.assertRaises(trusted_fetch.FetchDenied):
                    trusted_fetch._fetch_tarball(self.loop, REPO, BOT, "a" * 40, 1024)
        self.assertNotEqual(sent[0], sent[1])

    def test_a_late_401_for_an_old_token_never_drops_its_successor(self):
        # Review of #1781, F4: two requests carried the old token. The first 401 led to a new
        # one being minted while the second was in flight; the second's 401 arrives after.
        old = gh.token(self.loop, BOT)
        successor = []

        def answer(req, timeout=None):
            if not successor:                         # the other request's 401, and its retry
                github_app.refused(self.loop, BOT, old)
                successor.append(gh.token(self.loop, BOT))
            _http_error(401)(req, timeout)
        with _patches(answer):
            self.assertEqual(gh.request(self.loop, f"/repos/{REPO}").status, 401)
        self.assertNotEqual(successor[0], old)
        self.assertEqual(gh.token(self.loop, BOT), successor[0])     # kept in memory
        self.assertEqual(app_tokens.load(KEY)[0], successor[0])      # and for every process
        self.assertEqual(self.mints, 2)

    def test_a_hung_minter_holds_no_one_past_the_wait_or_a_gates_budget(self):
        # Review of #1781, F1: the lock was waited for without a bound, holding a gate past its
        # budget behind a holder that never let go.
        held = self.hold_lock(KEY)
        with mock.patch.object(app_tokens, "LOCK_WAIT_S", 0.3):
            start = time.monotonic()
            token = gh.token(self.loop, BOT)                          # no budget: a worker
            self.assertLess(time.monotonic() - start, 2)
            self.assertTrue(token.startswith("ghs_"))
        self.new_process()
        with gh.gate_scope(time.monotonic() + 0.2), \
                mock.patch.object(app_tokens, "LOCK_WAIT_S", 60):
            start = time.monotonic()
            with mock.patch.object(app_tokens, "load", return_value=None), \
                    self.assertRaises(gh.GateBudgetExceeded):
                gh.token(self.loop, BOT)
            self.assertLess(time.monotonic() - start, 2)
        held.set()

    def test_without_the_lock_nothing_shared_is_changed(self):
        # Review of #1781, F5: past the bounded wait, refused() still did load-compare-drop
        # unlocked, so a successor published between the load and the drop was deleted.
        old = gh.token(self.loop, BOT)
        before = app_tokens._file(KEY).read_bytes()
        held = self.hold_lock(KEY)
        successor = ("ghs_successor_" + "x" * 370, time.time() + 3600)

        def load(key):                                # another process publishes meanwhile
            app_tokens._file(key).write_text(json.dumps(
                {"token": successor[0], "expires": successor[1]}))
            return (old, successor[1])
        with mock.patch.object(app_tokens, "LOCK_WAIT_S", 0.2), \
                mock.patch.object(app_tokens, "load", side_effect=load):
            github_app.refused(self.loop, BOT, old)
        self.assertEqual(app_tokens._file(KEY).read_bytes(), before)   # never rewritten unlocked
        self.assertIsNone(app_tokens.load(KEY))       # but marked refused for everyone (F10)
        # And a token minted without the lock is this process's alone: never published.
        app_tokens.drop(KEY)
        self.new_process()
        with mock.patch.object(app_tokens, "LOCK_WAIT_S", 0.2):
            gh.token(self.loop, BOT)
        self.assertIsNone(app_tokens.load(KEY))
        held.set()
        self.assertNotEqual(gh.token(self.loop, BOT), old)          # refused: never used again

    def test_a_401_that_misses_the_lock_still_reaches_every_process(self):
        # Review of #1781, F10: a 401 handler that could not take the lock left the shared file
        # to the holder, and nothing else told the other processes: after the holder let go, a
        # new process loaded the dead token. It now leaves a mark beside the file.
        dead = gh.token(self.loop, BOT)
        held = self.hold_lock(KEY)
        with mock.patch.object(app_tokens, "LOCK_WAIT_S", 0.2):
            github_app.refused(self.loop, BOT, dead)
        held.set()                                    # the holder lets go, having done nothing
        self.assertEqual(json.loads(app_tokens._file(KEY).read_text())["token"], dead)
        marks = list(app_tokens.directory().glob(f"{app_tokens._file(KEY).name}.refused-*"))
        self.assertEqual(len(marks), 1)
        self.assertNotIn(dead, marks[0].name)         # a hash names it, never the token
        self.assertEqual(marks[0].read_bytes(), b"")
        script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from unittest import mock
from diaktoros import gh, gh_cache, github_app
gh_cache.enable_writes()
def as_app(loop, login, path, method="GET", body=None):
    if path.endswith("/installation"):
        return gh.Response({{"id": 9}}, "", 200, {{}})
    return gh.Response({{"token": "ghs_fresh_" + "x" * 380, "expires_at": {LATER!r}}}, "", 201, {{}})
loop = {{"repo": {REPO!r}, "read_token": {BOT!r}, "apps": {{{BOT!r}: {{"app_id": {APP_ID}, "key_file": "/k"}}}}}}
with mock.patch.object(github_app, "_as_app", side_effect=as_app):
    print(gh.token(loop, {BOT!r})[:16])
"""
        from diaktoros import config
        other = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env={**os.environ, "HERMES_HOME": str(config.home())},
                               stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(other.stdout.strip(), "ghs_fresh_xxxxxx", other.stderr)
        self.assertEqual(list(app_tokens.directory().glob("*.refused-*")), [])   # its save cleared it

    def test_threads_wait_for_a_token_within_their_own_bounds(self):
        # Review of #1781, F6: the bounded flock wait ran under one process-wide lock, so a
        # thread for another App, or with a shorter budget, waited behind a slow mint unbounded.
        release = threading.Event()
        self.slow[BOT] = release
        self.addCleanup(release.set)
        minting = threading.Thread(target=gh.token, args=(self.loop, BOT), daemon=True)
        minting.start()
        time.sleep(0.2)                               # it holds BOT's lock, mid-mint
        results = {}

        def other_app():
            with gh.gate_scope(time.monotonic() + 0.3):
                start = time.monotonic()
                results["other"] = (gh.token(self.loop, OTHER), time.monotonic() - start)

        def same_app():
            with gh.gate_scope(time.monotonic() + 0.2):
                start = time.monotonic()
                try:
                    gh.token(self.loop, BOT)
                    results["same"] = ("token", time.monotonic() - start)
                except gh.GateBudgetExceeded:
                    results["same"] = ("budget", time.monotonic() - start)
        threads = [threading.Thread(target=f, daemon=True) for f in (other_app, same_app)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertLess(results["other"][1], 1)       # another App never waits for this one
        self.assertEqual(results["same"][0], "budget")
        self.assertLess(results["same"][1], 1)        # a gate stops within its own budget
        release.set()
        minting.join(5)

    def test_a_401_recorded_while_a_thread_reads_the_shared_token_is_never_undone(self):
        # Review of #1781, F8: a thread read the shared token and checked it against the
        # refusals, then stored it; another thread's 401 landed in between, and the dead token
        # went back into memory for good. The reader is paused at its second take of the
        # process lock (where the check and the store now both happen), the 401 is recorded,
        # and the reader resumes.
        dead = gh.token(self.loop, BOT)
        self.new_process()
        real, paused, resume = github_app._lock, threading.Event(), threading.Event()
        reader = {}

        class Gate:
            taken = 0

            def __enter__(self):
                if threading.current_thread().name == "reader":
                    Gate.taken += 1
                    if Gate.taken == 2:
                        paused.set()
                        resume.wait(5)
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)
        with mock.patch.object(github_app, "_lock", Gate()), \
                mock.patch.object(app_tokens, "LOCK_WAIT_S", 0.2):
            thread = threading.Thread(name="reader", daemon=True,
                                      target=lambda: reader.setdefault("got", gh.token(self.loop, BOT)))
            thread.start()
            self.assertTrue(paused.wait(5))
            github_app.refused(self.loop, BOT, dead)                # another thread's 401
            resume.set()
            thread.join(5)
            self.assertNotEqual(reader["got"], dead)
            self.assertNotEqual(gh.token(self.loop, BOT), dead)

    def test_a_process_with_the_token_in_memory_follows_the_shared_file(self):
        # Review of #1781, F9: a process that had the shared token in memory never looked at
        # the file again, so after another process dropped it on a 401 and minted a successor,
        # it kept sending the dead token. Here the other process is a real one, with its own
        # memory: it reads the token, this process replaces it, and it reads again.
        script = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from unittest import mock
from diaktoros import gh, gh_cache, github_app
gh_cache.enable_writes()
def as_app(loop, login, path, method="GET", body=None):
    if path.endswith("/installation"):
        return gh.Response({{"id": 9}}, "", 200, {{}})
    return gh.Response({{"token": "ghs_child_" + "x" * 380, "expires_at": {LATER!r}}}, "", 201, {{}})
loop = {{"repo": {REPO!r}, "read_token": {BOT!r}, "apps": {{{BOT!r}: {{"app_id": {APP_ID}, "key_file": "/k"}}}}}}
with mock.patch.object(github_app, "_as_app", side_effect=as_app):
    print(gh.token(loop, {BOT!r})[:16], flush=True)
    sys.stdin.readline()                              # this process replaces the token meanwhile
    print(gh.token(loop, {BOT!r})[:16], flush=True)
"""
        from diaktoros import config
        env = {**os.environ, "HERMES_HOME": str(config.home())}
        child = subprocess.Popen([sys.executable, "-c", script], env=env, text=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.addCleanup(child.kill)
        first = child.stdout.readline().strip()
        self.assertEqual(first, "ghs_child_xxxxxx", child.stderr.read() if not first else "")
        app_tokens.drop(KEY)                                        # its 401, seen here
        app_tokens.save(KEY, "ghs_successor_" + "x" * 370, time.time() + 3600)
        out, err = child.communicate("go\n", timeout=30)
        self.assertEqual(out.strip(), "ghs_successor_xx", err)

    def test_a_mint_for_one_app_never_waits_for_another(self):
        held = self.hold_lock(("other[bot]", REPO, 1))
        with mock.patch.object(app_tokens, "LOCK_WAIT_S", 60):
            start = time.monotonic()
            gh.token(self.loop, BOT)
            self.assertLess(time.monotonic() - start, 2)
        held.set()

    def hold_lock(self, key):
        """Another minter for ``key`` that never lets go, through ``app_tokens.held`` itself (a
        flock conflicts across open files, in one process or many). Released at cleanup."""
        taken, release = threading.Event(), threading.Event()

        def minter():
            with app_tokens.held(key):
                taken.set()
                release.wait(30)
        thread = threading.Thread(target=minter, daemon=True)
        thread.start()
        self.assertTrue(taken.wait(5))
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        return release

KEY = (BOT.casefold(), REPO, APP_ID)


def _patches(urlopen):
    stack = contextlib.ExitStack()
    for patch in (mock.patch.object(gh, "API", "http://127.0.0.1:1"),
                  mock.patch("urllib.request.urlopen", side_effect=urlopen),
                  mock.patch("urllib.request.OpenerDirector.open", side_effect=urlopen),
                  mock.patch.dict(os.environ, {"DIAKTOROS_GH_STUB": "", "REVIEW_LOOP_GH_STUB": ""})):
        stack.enter_context(patch)
    return stack


def _http_error(code, sent=None):
    import email.message
    import io
    import urllib.error

    def raise_(req, timeout=None, *args, **kwargs):
        if sent is not None:
            sent.append(req.get_header("Authorization").split()[-1])
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
