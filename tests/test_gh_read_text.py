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
from diaktoros import gh, gh_text  # noqa: E402

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
        return gh_text.read_text(LOOP, "/repos/owner/one/actions/jobs/9/logs")

    def test_the_log_is_read_and_the_token_stays_on_the_api_host(self):
        self.assertEqual(self.read(), "line one\nthe failing line\n")
        api, storage = self.seen
        self.assertEqual(api["api"].get("Authorization"), f"token {SECRET}")
        self.assertNotIn("Authorization", storage)
        self.assertFalse(any(SECRET in str(v) for v in storage.values()), storage)

    def test_a_redirect_from_https_down_to_http_is_not_followed(self):
        handler = gh_text._TokenlessRedirect()
        req = gh_text.urllib.request.Request("https://api.github.com/x",
                                             headers={"Authorization": f"token {SECRET}"})
        with self.assertRaises(gh_text.urllib.error.HTTPError) as refused:
            handler.redirect_request(req, None, 302, "Found", {}, self.target)
        refused.exception.close()
        self.assertIn("leaves https", str(refused.exception))


class OneBinding(unittest.TestCase):
    """The text read lives in ``gh_text`` only, split out of ``gh`` at its size ceiling: ``gh`` keeps
    no second binding a patch could miss, and no caller still names the old path."""

    MOVED = ("read_text", "_TokenlessRedirect", "_TEXT_OPENER", "_count_failed_text")

    def test_gh_no_longer_holds_the_text_read(self):
        for name in self.MOVED:
            self.assertFalse(hasattr(gh, name), name)
            self.assertTrue(hasattr(gh_text, name), name)

    def test_no_caller_names_the_old_path(self):
        import re
        root = Path(__file__).resolve().parents[1]
        old = re.compile(r"\bgh\.(%s)\b|\(gh, [\"'](%s)[\"']" % ("|".join(self.MOVED), "|".join(self.MOVED)))
        found = [f"{path.relative_to(root)}:{n}" for top in ("diaktoros", "scripts", "tests")
                 for path in sorted((root / top).rglob("*.py")) if "__pycache__" not in path.parts
                 for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if old.search(line) and path.name != "test_gh_read_text.py"]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
