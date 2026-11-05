"""#1090: ``review_kick.kick`` delivers the reviewer gate the event GitHub did not send (#532).

The other tests mock ``kick`` and check its callers; these drive the real function through the
real ``routes.fire`` to a loopback HTTP server standing in for the gateway, and check what that
server received: the route's URL, the signature over the body, the event header and the
``review_requested`` payload. No real gateway, GitHub or ~/.hermes.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import review_kick  # noqa: E402

HEAD = "c" * 40
SECRET = "kick-secret"


class _Gateway(BaseHTTPRequestHandler):
    """Records each POST; answers with the server's ``status``."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.received.append({"path": self.path, "headers": self.headers,
                                     "body": body})
        self.send_response(self.server.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Kick(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Gateway)
        self.server.received = []
        self.server.status = 202
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        subs = Path(tmp.name) / "webhook_subscriptions.json"
        subs.write_text(json.dumps({"review": {"secret": SECRET, "profile": "rev",
                                               "events": ["pull_request"]}}))
        env = mock.patch.dict(os.environ, {"DIAKTOROS_SUBS": str(subs)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = {"id": "widgets", "repo": "acme/widgets", "base": "main",
                     "reviewer_seat": "reviewer-bot",
                     "seats": {"reviewer": {"profile": "rev", "route": "review"}},
                     "host": f"http://127.0.0.1:{self.server.server_port}"}
        self.pr = {"number": 7, "title": "Fix widgets", "html_url": "https://example.test/7",
                   "user": {"login": "dev"}, "base": {"ref": "release"},
                   "head": {"sha": "0" * 40, "ref": "fix-7"}}

    def test_kick_delivers_signed_review_requested_to_reviewer_route(self):
        sent = review_kick.kick(self.loop, self.pr, HEAD, "dev", "kick-7")
        self.assertTrue(sent)
        self.assertEqual(len(self.server.received), 1)
        got = self.server.received[0]
        self.assertEqual(got["path"], "/p/rev/webhooks/review")
        self.assertEqual(got["headers"]["X-GitHub-Event"], "pull_request")
        expected_sig = "sha256=" + hmac.new(SECRET.encode(), got["body"],
                                            hashlib.sha256).hexdigest()
        self.assertEqual(got["headers"]["X-Hub-Signature-256"], expected_sig)
        self.assertTrue(got["headers"]["X-GitHub-Delivery"].startswith("kick-7"))
        payload = json.loads(got["body"])
        self.assertEqual(payload, review_kick.review_requested(self.loop, self.pr, HEAD, "dev"))
        self.assertEqual(payload["action"], "review_requested")
        self.assertEqual(payload["requested_reviewer"], {"login": "reviewer-bot"})
        self.assertEqual(payload["repository"], {"full_name": "acme/widgets"})
        self.assertEqual(payload["sender"], {"login": "dev"})
        self.assertEqual(payload["number"], 7)
        short = payload["pull_request"]
        self.assertEqual(short["head"], {"sha": HEAD, "ref": "fix-7"})   # the kicked head
        self.assertEqual(short["base"], {"ref": "release"})
        self.assertFalse(short["draft"])

    def test_kick_without_reviewer_route_sends_nothing(self):
        for seats in ({}, {"reviewer": {}}, {"reviewer": {"route": ""}}):
            with self.subTest(seats=seats):
                loop = dict(self.loop, seats=seats)
                self.assertFalse(review_kick.kick(loop, self.pr, HEAD, "dev", "kick-7"))
        self.assertEqual(self.server.received, [])

    def test_kick_is_false_when_gateway_refuses(self):
        self.server.status = 403
        self.assertFalse(review_kick.kick(self.loop, self.pr, HEAD, "dev", "kick-7"))
        self.assertEqual(len(self.server.received), 1)

    def test_kick_is_false_when_route_is_not_registered(self):
        loop = dict(self.loop, seats={"reviewer": {"route": "missing"}})
        self.assertFalse(review_kick.kick(loop, self.pr, HEAD, "dev", "kick-7"))
        self.assertEqual(self.server.received, [])

    def test_base_falls_back_to_loop_base(self):
        pr = dict(self.pr, base={})
        self.assertTrue(review_kick.kick(self.loop, pr, HEAD, "dev", "kick-7"))
        payload = json.loads(self.server.received[0]["body"])
        self.assertEqual(payload["pull_request"]["base"], {"ref": "main"})


class AlreadyRequested(unittest.TestCase):
    loop = {"reviewer_seat": "Reviewer-Bot"}

    def test_pending_seat_matches_case_insensitively(self):
        pr = {"requested_reviewers": [{"login": "other"}, {"login": "reviewer-bot"}]}
        self.assertTrue(review_kick.already_requested(self.loop, pr))

    def test_not_pending(self):
        for pr in (None, [], {}, {"requested_reviewers": None},
                   {"requested_reviewers": [{"login": "other"}, "reviewer-bot"]}):
            with self.subTest(pr=pr):
                self.assertFalse(review_kick.already_requested(self.loop, pr))

    def test_no_seat_is_never_pending(self):
        pr = {"requested_reviewers": [{"login": ""}]}
        self.assertFalse(review_kick.already_requested({}, pr))


if __name__ == "__main__":
    unittest.main()
