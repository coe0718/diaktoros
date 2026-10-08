"""A job log is read through GitHub's redirect without handing the token to the log host.

GitHub answers `/actions/jobs/{id}/logs` with a redirect to the file in its log storage, another
host. urllib copies `Authorization` onto a redirected request, so a plain `urlopen` sends the read
token there. Two local servers stand in: the "API" on 127.0.0.1 redirects to "storage" on
localhost, which records the headers it was sent.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.server
import sys
import threading
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import gh  # noqa: E402

LOOP = {"repo": "owner/one", "read_token": "reader"}
SECRET = "ghp_not_a_real_token"


def serve(handler) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ReadText(unittest.TestCase):
    def setUp(self):
        self.seen: list[dict] = []
        seen = self.seen

        class Storage(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                body = b"line one\nthe failing line\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.storage = serve(Storage)
        self.addCleanup(self.storage.server_close)
        self.addCleanup(self.storage.shutdown)
        self.target = f"http://localhost:{self.storage.server_port}/logs/blob?sig=x"
        target = self.target

        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({"api": dict(self.headers)})
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.api = serve(Api)
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        for patch in (mock.patch.object(gh, "API", f"http://127.0.0.1:{self.api.server_port}"),
                      mock.patch.object(gh, "token", return_value=SECRET),
                      mock.patch.dict("os.environ", {"DIAKTOROS_GH_STUB": "", "REVIEW_LOOP_GH_STUB": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def read(self):
        return gh.read_text(LOOP, "/repos/owner/one/actions/jobs/9/logs")

    def test_the_log_is_read_and_the_token_stays_on_the_api_host(self):
        self.assertEqual(self.read(), "line one\nthe failing line\n")
        api, storage = self.seen
        self.assertEqual(api["api"].get("Authorization"), f"token {SECRET}")
        self.assertNotIn("Authorization", storage)
        self.assertFalse(any(SECRET in str(v) for v in storage.values()), storage)

    def test_a_redirect_from_https_down_to_http_is_not_followed(self):
        handler = gh._TokenlessRedirect()
        req = gh.urllib.request.Request("https://api.github.com/x",
                                        headers={"Authorization": f"token {SECRET}"})
        with self.assertRaises(gh.urllib.error.HTTPError) as refused:
            handler.redirect_request(req, None, 302, "Found", {}, self.target)
        refused.exception.close()
        self.assertIn("leaves https", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
