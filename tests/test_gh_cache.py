"""#1646: a GET whose answer carried an ETag asks GitHub whether it changed (``gh_cache``).

A local server stands in for the API: it gives each body an ETag and answers ``If-None-Match``
for the current one with ``304 Not Modified`` and no body, as GitHub does. A 304 is free on
GitHub's rate limit, and it is GitHub's word that the stored body is current.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.server
import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import gh, gh_cache  # noqa: E402

LOOP = {"repo": "owner/one", "read_token": "reader"}
PATH = "/repos/owner/one/pulls/7/reviews?per_page=100"


class Cache(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(gh_cache.directory(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, gh_cache.directory(), True)
        self.body, self.etag, self.seen = [{"id": 1}], '"v1"', []
        self.scopes = "repo, read:repo_hook"          # /user's classic-PAT header; "" = fine-grained
        test = self

        class Api(http.server.BaseHTTPRequestHandler):
            def answer(self):
                test.seen.append((self.command, self.headers.get("If-None-Match"),
                                  self.headers.get("Authorization")))
                if self.path == "/user":
                    body = json.dumps({"login": "reader"}).encode()
                    if self.headers.get("If-None-Match") == '"me"':
                        self.send_response(304)               # as GitHub might: no scopes header
                        self.send_header("ETag", '"me"')
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("ETag", '"me"')
                    if test.scopes:
                        self.send_header("X-OAuth-Scopes", test.scopes)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.command == "GET" and self.headers.get("If-None-Match") == test.etag:
                    self.send_response(304)
                    self.send_header("ETag", test.etag)
                    self.send_header("X-RateLimit-Remaining", "4999")
                    self.end_headers()
                    return
                body = json.dumps(test.body).encode()
                self.send_response(200)
                if test.etag:
                    self.send_header("ETag", test.etag)
                self.send_header("X-RateLimit-Remaining", "5000")
                self.send_header("X-Only-On-The-200", "stale")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = answer

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for patch in (mock.patch.object(gh_cache, "_WRITES", True),      # a writer process
                      mock.patch.object(gh, "API", f"http://127.0.0.1:{server.server_port}"),
                      mock.patch.object(gh, "token", side_effect=lambda _loop, login=None:
                                        f"tok-{login or 'reader'}"),
                      mock.patch.dict("os.environ", {"DIAKTOROS_GH_STUB": "",
                                                     "REVIEW_LOOP_GH_STUB": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def get(self, login=None):
        return gh.request(LOOP, PATH, login=login)

    def test_an_unchanged_answer_is_a_304_serving_the_stored_body(self):
        first = self.get()
        self.assertEqual((first.data, first.status), ([{"id": 1}], 200))
        second = self.get()
        self.assertEqual((second.data, second.error, second.status), ([{"id": 1}], "", 200))
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, '"v1"'])
        self.assertEqual(second.headers.get("x-ratelimit-remaining"), "4999")   # the fresh ones
        # #1653 review, F1: a 304 vouches for the body only; no header of the old 200 rides along.
        self.assertNotIn("x-only-on-the-200", second.headers)

    def test_a_changed_answer_replaces_the_stored_one(self):
        self.get()
        self.body, self.etag = [{"id": 1}, {"id": 2}], '"v2"'
        self.assertEqual(self.get().data, [{"id": 1}, {"id": 2}])
        self.assertEqual(self.get().data, [{"id": 1}, {"id": 2}])
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, '"v1"', '"v2"'])

    def test_one_logins_entry_is_never_another_logins(self):
        self.get()
        self.get(login="reviewer")
        self.assertEqual([(inm, auth) for _m, inm, auth in self.seen],
                         [(None, "token tok-reader"), (None, "token tok-reviewer")])

    def test_an_answer_without_an_etag_is_not_kept(self):
        self.etag = ""
        self.get()
        self.get()
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, None])

    def test_a_write_is_never_conditional_and_never_kept(self):
        gh.request(LOOP, PATH, method="POST", body={"x": 1})
        self.get()
        gh.request(LOOP, PATH, method="POST", body={"x": 1})       # a GET's entry is there now
        self.assertEqual([(m, inm) for m, inm, _a in self.seen],
                         [("POST", None), ("GET", None), ("POST", None)])

    def test_a_corrupt_entry_is_a_plain_get(self):
        self.get()
        [entry] = [f for f in gh_cache.directory().iterdir()]
        entry.write_text("{not json")
        self.assertEqual(self.get().data, [{"id": 1}])
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, None])

    def test_a_rotated_token_reads_its_own_scopes_in_doctor(self):
        # #1653 review, F1: /user is a credential probe and is never conditional, so after a
        # classic PAT is rotated to a fine-grained one, doctor reads the new token's headers.
        from diaktoros.doctor import identity_checks
        loop = {**LOOP, "seats": {}}
        def scopes():
            with mock.patch.object(identity_checks, "_token_roles",
                                   return_value={"reader": ["reader"]}):
                checks = identity_checks.check_token_expiry_scopes(loop, offline=False)
            return {c.name: c.status for c in checks}["token-scopes:reader"]
        self.assertEqual(scopes(), "verified")
        self.scopes = ""                                       # rotated: a fine-grained PAT
        self.assertEqual(scopes(), "skipped")
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, None])

    def test_the_size_bound_is_bytes_as_stored(self):
        # #1653 review, F2: the bound counts the stored entry's bytes, multibyte text included.
        self.body = ["é" * 120]                                # 240 bytes of UTF-8 in the body
        with mock.patch.object(gh_cache, "MAX_BYTES", 250):
            self.get()                                         # the entry is over 250 bytes
            self.assertFalse(gh_cache.directory().exists() and any(gh_cache.directory().iterdir()))
        with mock.patch.object(gh_cache, "MAX_BYTES", 300):
            self.get()
            [entry] = list(gh_cache.directory().iterdir())
            self.assertLessEqual(entry.stat().st_size, 300)
            self.assertEqual(self.get().data, ["é" * 120])
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, None, '"v1"'])

    def test_a_non_ascii_entry_is_read_as_utf8_under_an_ascii_locale(self):
        # #1703: store writes UTF-8; lookup must read UTF-8 too, not the locale's encoding (cron
        # or systemd with LANG unset), or the entry is "corrupt" and the cache silently off.
        import os
        import subprocess
        gh_cache.store("reader", PATH, ["é ✓"], {"etag": '"v1"'})
        child = ("import json, sys; sys.path.insert(0, sys.argv[1]); "
                 "from diaktoros import gh_cache; import locale; "
                 "print(locale.getpreferredencoding(False)); "
                 "print(json.dumps(gh_cache.lookup('reader', sys.argv[2])))")
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
               "PYTHONCOERCECLOCALE": "0", "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-c", child, str(Path(__file__).resolve().parents[1]),
                              PATH], env=env, capture_output=True, text=True, check=True).stdout
        encoding, found = out.splitlines()
        self.assertNotIn("utf", encoding.lower().replace("-", ""))   # the locale is not UTF-8
        self.assertEqual(json.loads(found), {"etag": '"v1"', "data": ["é ✓"]})

    def test_a_read_only_process_never_stores_marks_or_prunes(self):
        # #1653 review, F3: doctor, explain, status and the Desktop routes promise to write
        # nothing. Writes are off unless the process is a writer (enable_writes).
        self.get()                                             # a writer stores the entry
        [entry] = list(gh_cache.directory().iterdir())
        old = time.time() - gh_cache.KEEP_S - 60
        import os
        os.utime(entry, (old, old))
        with mock.patch.object(gh_cache, "_WRITES", False):
            self.assertEqual(self.get().data, [{"id": 1}])     # it still uses the entry (a 304)
            self.assertEqual(entry.stat().st_mtime, old)       # ...but never marks it,
            self.assertEqual(gh_cache.prune(), 0)              # ...never prunes,
            gh.request(LOOP, PATH + "&page=2")
            self.assertEqual(list(gh_cache.directory().iterdir()), [entry])   # never stores
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, '"v1"', None])

    def test_only_the_writer_processes_turn_writes_on(self):
        import re
        root = Path(__file__).resolve().parents[1]
        found = sorted(str(f.relative_to(root)) for f in
                       [*(root / "diaktoros").rglob("*.py"), *(root / "scripts").glob("*.py")]
                       if re.search(r"gh_cache\.enable_writes\(\)", f.read_text()))
        # The gates through gate.context, and the watchdog; a worker is one by its env.
        self.assertEqual(found, ["diaktoros/gate.py", "scripts/watchdog.py"])

    def test_a_worker_never_creates_the_cache_directory(self):
        from diaktoros import hostdirs
        with mock.patch.dict("os.environ", {hostdirs.WORKER_ENV: "1"}), \
                mock.patch.object(gh_cache, "_WRITES", False):     # a worker is a writer anyway
            self.get()
            self.assertFalse(gh_cache.directory().exists())
            gh_cache.directory().mkdir(parents=True)           # the host made it: a worker may
            self.get()
            self.assertEqual(len(list(gh_cache.directory().iterdir())), 1)

    def test_a_stored_etag_that_is_no_header_value_is_a_plain_get(self):
        # #1653 review, F4: valid JSON holding an ETag with a newline is never sent.
        self.get()
        [entry] = list(gh_cache.directory().iterdir())
        entry.write_text(json.dumps({"etag": '"v1"\r\nX-Injected: 1', "data": ["forged"]}))
        response = self.get()
        self.assertEqual((response.data, response.error), ([{"id": 1}], ""))
        self.assertEqual([inm for _m, inm, _a in self.seen], [None, None])

    def test_entries_are_private_and_hold_no_token(self):
        self.get()
        [entry] = list(gh_cache.directory().iterdir())
        self.assertEqual(entry.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("tok-reader", entry.read_text())

    def test_an_entry_unused_for_two_days_is_pruned_and_a_304_keeps_it(self):
        self.get()
        [entry] = list(gh_cache.directory().iterdir())
        old = time.time() - gh_cache.KEEP_S - 60
        import os
        os.utime(entry, (old, old))
        self.get()                                              # a 304 marks it used
        self.assertEqual(gh_cache.prune(), 0)
        os.utime(entry, (old, old))
        self.assertEqual(gh_cache.prune(), 1)
        self.assertEqual(list(gh_cache.directory().iterdir()), [])


if __name__ == "__main__":
    unittest.main()
