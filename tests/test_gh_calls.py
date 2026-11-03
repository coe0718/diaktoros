"""The host's own count of its GitHub calls (``gh_calls``) and ``hermes dk calls``.

A local server stands in for the API, as in ``test_gh_cache``: a 200 with an ETag and GitHub's
rate-limit headers, a 304 for the current ETag, and a 403 once the limit is spent. Every call
goes through the real ``gh.request``, so a call that stops being counted turns a test red.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from diaktoros import gh, gh_cache, gh_calls, hostdirs  # noqa: E402

LOOP = {"repo": "owner/one", "read_token": "reader"}


class Counted(unittest.TestCase):
    def setUp(self):
        for kept in (gh_calls.directory(), gh_cache.directory()):
            shutil.rmtree(kept, ignore_errors=True)
            self.addCleanup(shutil.rmtree, kept, True)
        gh_calls._counts.clear()
        gh_calls._limits.clear()
        self.addCleanup(gh_calls._counts.clear)
        self.addCleanup(gh_calls._limits.clear)
        test, self.used = self, 100

        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.used += 1
                if "/to-storage/" in self.path:      # GitHub's answer to a log read: a redirect
                    where = self.path.rsplit("/to-storage/", 1)[1]
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/log" if where == "down"
                                     else f"/storage/{where}")
                    self.send_header("X-RateLimit-Used", str(test.used))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.endswith("/limited"):
                    self.send_response(403)
                    self.send_header("X-RateLimit-Remaining", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.send_header("ETag", '"v1"')
                    self.end_headers()
                    return
                body = b'[{"id": 1}]'
                self.send_response(200)
                self.send_header("ETag", '"v1"')
                for name, value in (("Limit", "5000"), ("Used", str(test.used)),
                                    ("Remaining", str(5000 - test.used)),
                                    ("Reset", "1793671000"), ("Resource", "core")):
                    self.send_header(f"X-RateLimit-{name}", value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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

    def get(self, path, login=None):
        return gh.request(LOOP, path, login=login)

    def counted(self):
        gh_calls.flush()
        return gh_calls.summary(time.time() - 3600)

    def test_each_call_is_counted_by_login_shape_and_outcome(self):
        self.get("/repos/owner/one/pulls/7/reviews?per_page=100&page=1")
        self.get("/repos/owner/one/pulls/7/reviews?per_page=100&page=1")       # a 304
        self.get("/repos/owner/one/pulls/8/reviews?per_page=100&page=2")
        self.get("/repos/owner/one/actions/jobs/9/limited")
        reader = self.counted()["reader"]
        self.assertEqual((reader["spent"], reader["free"], reader["limited"]), (2, 1, 1))
        self.assertEqual(reader["paths"][0],
                         ["GET", "/repos/R/pulls/N/reviews?page&per_page", "spent", 2])
        self.assertIn(["GET", "/repos/R/pulls/N/reviews?page&per_page", "free", 1],
                      reader["paths"])
        # GitHub's own figure, from the newest answer that carried it.
        self.assertEqual((reader["limit"]["used"], reader["limit"]["limit"]), ("103", "5000"))

    def test_a_job_log_read_is_counted_too(self):
        self.assertIsNotNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/5/logs"))
        self.assertEqual(self.counted()["reader"]["paths"],
                         [["GET", "/repos/R/actions/jobs/N/logs", "spent", 1]])

    def test_a_failed_job_log_read_is_counted_once_with_its_outcome(self):
        # Review of #1758, F2: a refused, unanswered or unsent log read is counted too.
        self.assertIsNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/5/limited"))
        with mock.patch.object(gh, "API", "http://127.0.0.1:1"):     # nothing listens there
            self.assertIsNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/6/logs"))
        with mock.patch.object(gh, "token", side_effect=gh.GitHubError("no token file")):
            self.assertIsNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/7/logs"))
        reader = self.counted()["reader"]
        self.assertEqual((reader["limited"], reader["error"], reader["unsent"], reader["spent"]),
                         (1, 1, 1, 0))

    def test_a_log_read_github_redirected_is_spent_whatever_the_storage_host_does(self):
        # Review of #1758, F3: GitHub answered, so a storage host that is down or refuses
        # (a 403 that looks like a rate limit) does not turn the API call into error or limited.
        self.assertIsNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/5/to-storage/down"))
        self.assertIsNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/6/to-storage/limited"))
        self.assertIsNotNone(gh.read_text(LOOP, "/repos/owner/one/actions/jobs/7/to-storage/log"))
        reader = self.counted()["reader"]
        self.assertEqual((reader["spent"], reader["error"], reader["limited"]), (3, 0, 0))
        self.assertEqual(reader["limit"]["used"], "104")     # from the last redirect, not storage

    def test_logins_are_kept_apart_and_a_missing_credential_is_unsent(self):
        self.get("/repos/owner/one/pulls/7", login="reviewer")
        with mock.patch.object(gh, "token", side_effect=gh.GitHubError("no token file")):
            self.get("/repos/owner/one/pulls/7", login="fixer")
        counted = self.counted()
        self.assertEqual(counted["reviewer"]["spent"], 1)
        self.assertEqual((counted["fixer"]["spent"], counted["fixer"]["unsent"]), (0, 1))

    def test_the_rate_limit_endpoint_is_free_and_no_answer_is_an_error(self):
        self.get("/rate_limit")
        with mock.patch.object(gh, "API", "http://127.0.0.1:1"):     # nothing listens there
            self.get("/repos/owner/one/pulls/7")
        reader = self.counted()["reader"]
        self.assertEqual((reader["free"], reader["error"], reader["spent"]), (1, 1, 0))

    def test_a_process_that_writes_nothing_counts_nothing(self):
        with mock.patch.object(gh_cache, "_WRITES", False):
            self.get("/repos/owner/one/pulls/7")
            gh_calls.flush()
        self.assertFalse(gh_calls.directory().exists())

    def test_a_worker_counts_only_into_a_directory_the_host_made(self):
        with mock.patch.object(gh_cache, "_WRITES", False), \
                mock.patch.object(hostdirs, "in_worker", return_value=True):
            self.get("/repos/owner/one/pulls/7")
            gh_calls.prepare()                   # a worker never makes it, even when asked
            gh_calls.flush()
            self.assertFalse(gh_calls.directory().exists())

    def test_the_host_makes_the_directory_before_it_starts_a_worker(self):
        # Review of #1758, F1: on a fresh host no writer has flushed yet, so nothing else has
        # made the directory. Starting a worker (the real _spawn) makes it, before the worker
        # runs, and the worker's first call is counted.
        import tempfile
        from diaktoros.run_supervisor import Supervisor, workers
        with tempfile.TemporaryDirectory() as tmp:
            sup = Supervisor(Path(tmp) / "runs.sqlite", fixture_mode=True)
            with mock.patch.object(workers.subprocess, "Popen") as popen:
                sup._spawn()
            popen.assert_called_once()
        self.assertTrue(gh_calls.directory().is_dir())
        with mock.patch.object(gh_cache, "_WRITES", False), \
                mock.patch.object(hostdirs, "in_worker", return_value=True):
            self.get("/repos/owner/one/pulls/8")
            gh_calls.flush()
        self.assertEqual(gh_calls.summary(time.time() - 3600)["reader"]["spent"], 1)

    def test_a_long_lived_process_writes_its_count_as_it_goes(self):
        for n in range(gh_calls.FLUSH_EVERY):
            self.get(f"/repos/owner/one/pulls/{n}")
        files = list(gh_calls.directory().glob("*.json"))      # no flush called by the test
        self.assertEqual(len(files), 1)
        self.assertEqual(gh_calls.summary(time.time() - 3600)["reader"]["spent"],
                         gh_calls.FLUSH_EVERY)

    def test_processes_are_summed_and_old_hours_and_bad_files_are_left_out(self):
        gh_calls.directory().mkdir(parents=True)
        now = int(time.time() // 3600 * 3600)
        for name, hour in (("a.json", now), ("b.json", now), ("old.json", now - 7200)):
            (gh_calls.directory() / name).write_text(json.dumps(
                {"counts": [[hour, "reader", "GET", "/repos/R/pulls/N", "spent", 5]]}))
        (gh_calls.directory() / "bad.json").write_text("{not json")
        self.assertEqual(gh_calls.summary(now)["reader"]["spent"], 10)

    def test_old_files_are_pruned(self):
        self.get("/repos/owner/one/pulls/7")
        gh_calls.flush()
        self.assertEqual(gh_calls.prune(time.time() + gh_calls.KEEP_S + 1), 1)
        self.assertEqual(list(gh_calls.directory().iterdir()), [])

    def test_the_calls_command_lists_the_costliest_paths_and_githubs_figure(self):
        for _ in range(3):
            self.get("/repos/owner/one/actions/jobs/1")                   # 1 spent, 2 free
        self.get("/repos/owner/one/actions/jobs/2")
        self.get("/repos/owner/one/pulls/7")
        gh_calls.flush()
        rc, text = t.run_cli(t.parser_for({}).parse_args(["calls", "--top", "1"]))
        self.assertEqual(rc, 0, text)
        self.assertIn("reader: 3 spent · 2 free", text)
        self.assertIn("GitHub at", text)
        self.assertIn("of 5000 used (core)", text)
        self.assertIn("      2  GET    /repos/R/actions/jobs/N  (spent)", text)
        self.assertNotIn("/repos/R/pulls/N", text)                        # --top 1
        rc, raw = t.run_cli(t.parser_for({}).parse_args(["calls", "--json"]))
        self.assertEqual(json.loads(raw)["logins"]["reader"]["spent"], 3)


class Shape(unittest.TestCase):
    def test_what_varies_is_taken_out(self):
        sha = "a" * 40
        for path, shape in (
                ("/repos/o/r/pulls/12/reviews?per_page=100&page=3", "/repos/R/pulls/N/reviews?page&per_page"),
                (f"/repos/o/r/commits/{sha}/check-runs", "/repos/R/commits/SHA/check-runs"),
                ("/repos/o/r/git/refs/heads/diaktoros/issue-1", "/repos/R/git/refs/heads/REF"),
                (f"/repos/o/r/compare/{sha}...feat", "/repos/R/compare/X"),
                ("/users/dk-reader[bot]", "/users/X"),
                ("/rate_limit", "/rate_limit")):
            with self.subTest(path=path):
                self.assertEqual(gh_calls.shape(path), shape)


if __name__ == "__main__":
    unittest.main()
