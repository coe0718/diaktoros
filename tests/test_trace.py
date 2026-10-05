#!/usr/bin/env python3
"""#216: ``trace`` runs one webhook through its gate as a dry run.

The gate under trace is the real script, so its answer must be the live gate's own. These run the
same payload through ``scripts/gate_*.py`` directly (as the gateway would) and through ``trace``,
and compare: the same log lines, the same decision. A trace must also leave every byte of the
loop's real state, config, route registry and GitHub world as it found them, and turn anything that
would leave the machine into a "would …" line instead.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import io
import json
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import config, gh, trace  # noqa: E402


def payload(action: str, sender: str, *, requested: str | None = None, number: int = 7) -> dict:
    body = {"action": action, "number": number, "pull_request": t.pr(number),
            "repository": {"full_name": t.REPO}, "sender": {"login": sender}}
    if requested:
        body["requested_reviewer"] = {"login": requested}
    return body


def digest(*roots: pathlib.Path) -> str:
    sha = hashlib.sha256()
    for root in roots:
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_file():
                sha.update(str(path).encode())
                sha.update(path.read_bytes())
    return sha.hexdigest()


def gate_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("[review-loop]")]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={"7": t.pr(7)})
        self.loop = config.load_id("widgets")

    def real(self) -> list[pathlib.Path]:
        return [t.STATE_DIR, t.LOOPS_DIR, t.SUBS, t.WORLD_FILE, t.HOME]

    def trace(self, body: dict, event: str = "pull_request", role: str = "reviewer") -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(trace.run(self.loop, body, event, role), 0)
        return out.getvalue()

    def live(self, body: dict, script: str = "gate_reviewer.py") -> list[str]:
        kind, out, err = t.run(script, body)
        self.assertEqual(kind, "SILENT", out)
        return gate_lines(err)


class TraceIsTheLiveGate(Base):
    def test_a_declined_request_names_the_gates_own_reason(self):
        body = payload("review_requested", "stranger", requested=t.SEAT)
        out = self.trace(body)
        self.assertIn("outcome:   declined — sender stranger is not a fixer", out)
        self.assertIn("sender stranger", out)

    def test_trace_and_the_live_gate_say_the_same_thing(self):
        cases = [payload("review_requested", "stranger", requested=t.SEAT),
                 payload("review_requested", t.FIXER, requested="someone-else"),
                 payload("synchronize", t.FIXER),
                 payload("review_requested", t.FIXER, requested=t.SEAT)]
        for body in cases:
            with self.subTest(action=body["action"], sender=body["sender"]["login"]):
                t.reset(prs={"7": t.pr(7)})
                traced = gate_lines(self.trace(body))
                t.reset(prs={"7": t.pr(7)})
                self.assertEqual(traced, self.live(body))

    def test_nothing_real_changes_and_effects_are_reported_not_done(self):
        body = payload("review_requested", t.FIXER, requested=t.SEAT)
        before = digest(*self.real())
        out = self.trace(body)
        self.assertEqual(digest(*self.real()), before, "a trace changed the loop's real state")
        self.assertIn("answer:    [SILENT]", out)
        self.assertIn("outcome:   held — #7 @ aaaaaaa reviewer held: isolated worker unavailable", out)
        # The drain the gate runs is reported, not performed, and its refusal is not a gate line.
        self.assertIn("would start: ", out)
        self.assertNotIn("trace: dry run", out)

    def test_an_eligible_event_with_a_runtime_file_would_start_a_run(self):
        runtime = t.HOME / "review-loop-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(json.dumps({"source": str(t.TMP), "venv": str(t.TMP),
                                       "runtime": str(t.TMP), "rust": str(t.TMP)}))
        runtime.chmod(0o600)
        self.addCleanup(runtime.unlink, missing_ok=True)
        before = digest(*self.real())
        out = self.trace(payload("review_requested", t.FIXER, requested=t.SEAT))
        self.assertEqual(digest(*self.real()), before)
        self.assertIn("outcome:   would", out)
        # The isolated worker's launch is reported, never performed.
        self.assertRegex(out, r"would (start|queue)")


class HarnessSeams(unittest.TestCase):
    """#222: each of the harness's refusals is pinned, not only the process launch.

    A probe stands in for the gate and tries all three ways out: a non-GET ``urlopen`` (how a
    route POST or an observer notice leaves), a ``gh`` write, and a GET, which must
    pass. A loopback server counts what actually arrives.
    """

    def test_a_post_and_a_github_write_are_recorded_never_sent_and_a_get_passes(self):
        import http.server
        import subprocess
        import tempfile
        import threading
        from review_loop import util
        arrived = []

        class Server(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                arrived.append("GET")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self):
                arrived.append("POST")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/hook"
        with tempfile.TemporaryDirectory() as tmp:
            probe = pathlib.Path(tmp) / "probe_gate.py"
            probe.write_text(f"""
import urllib.request
from review_loop import gh
try:
    urllib.request.urlopen(urllib.request.Request({url!r}, data=b"{{}}", method="POST"), timeout=5)
    print("post: sent")
except Exception as exc:
    print("post: refused", type(exc).__name__)
answer = gh._request({{"read_token": "reader"}}, "/repos/acme/widgets/pulls/7/requested_reviewers",
                     "POST", {{"reviewers": ["x"]}}, "reader", 5)
print("gh:", answer.error)
with urllib.request.urlopen({url!r}, timeout=5) as resp:
    print("get:", resp.read().decode())
""")
            report = pathlib.Path(tmp) / "effects.json"
            env = util.leak_guard_env({k: v for k, v in os.environ.items()
                                       if k != "REVIEW_LOOP_GH_STUB"})
            proc = subprocess.run([sys.executable, "-c", util.leak_guard_code(trace._HARNESS),
                                   str(probe), str(trace.PLUGIN), str(report)],
                                  input="{}", capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            effects = json.loads(report.read_text())
        self.assertIn("post: refused URLError", proc.stdout)
        self.assertIn("gh: trace: dry run, not sent", proc.stdout)
        self.assertIn("get: ok", proc.stdout)
        self.assertEqual(arrived, ["GET"], "only the read may leave the machine")
        self.assertIn(["send", "POST 127.0.0.1/hook"], effects)
        self.assertIn(["github", "POST /repos/acme/widgets/pulls/7/requested_reviewers"], effects)


class Delivery(unittest.TestCase):
    loop = {"id": "widgets", "repo": "acme/widgets", "read_token": "reader",
            "seats": {"reviewer": {"route": "widgets-review"}, "fixer": {"route": "widgets-fix"}},
            "adjudicator": {}}

    def test_a_guid_is_found_on_the_loops_own_hook(self):
        hooks = [{"id": 5, "config": {"url": "https://gw.example/webhooks/other-route"}},
                 {"id": 9, "config": {"url": "https://gw.example/p/critic/webhooks/widgets-review"}}]
        seen = []

        def api(loop, path, method="GET", body=None, login=None):
            seen.append(path)
            if path.startswith("/repos/acme/widgets/hooks/9/deliveries?"):
                return [{"id": 77, "guid": "abc-guid"}]
            if path == "/repos/acme/widgets/hooks/9/deliveries/77":
                return {"event": "pull_request", "request": {"payload": {"action": "opened"}}}
            return []
        with mock.patch.object(gh, "hooks_read", return_value=(hooks, "")), \
                mock.patch.object(gh, "api", side_effect=api):
            body, event, route = trace.fetch_delivery(self.loop, "abc-guid", "admin")
        self.assertEqual((body, event, route), ({"action": "opened"}, "pull_request",
                                                "widgets-review"))
        self.assertFalse(any("/hooks/5/" in path for path in seen), "a foreign hook was read")

    def test_an_unknown_delivery_or_unreadable_hooks_is_a_clear_refusal(self):
        with mock.patch.object(gh, "hooks_read", return_value=(None, "HTTP 404")), \
                self.assertRaises(trace.TraceError) as caught:
            trace.fetch_delivery(self.loop, "1", None)
        self.assertIn("--admin-token", str(caught.exception))
        with mock.patch.object(gh, "hooks_read", return_value=([], "")), \
                self.assertRaises(trace.TraceError):
            trace.fetch_delivery(self.loop, "nope", None)

    def test_the_route_or_event_picks_the_gate(self):
        self.assertEqual(trace.role_for(self.loop, "pull_request"), "reviewer")
        self.assertEqual(trace.role_for(self.loop, "pull_request_review"), "fixer")
        self.assertEqual(trace.role_for(self.loop, "x", "widgets-fix"), "fixer")
        for event, route in (("issues", None), ("pull_request", "not-ours")):
            with self.subTest(event=event, route=route), self.assertRaises(trace.TraceError):
                trace.role_for(self.loop, event, route)

    def test_an_issues_delivery_reaches_the_triage_gate_only_when_triage_is_on(self):
        with self.assertRaises(trace.TraceError) as caught:     # widgets has no triage route
            trace.role_for(self.loop, "issues")
        self.assertIn("no triage route", str(caught.exception))
        on = {**self.loop, "triage": {"route": "widgets-triage"}}
        self.assertEqual(trace.role_for(on, "issues"), "triage")
        self.assertEqual(trace.role_for(on, "x", "widgets-triage"), "triage")

    def test_the_facts_of_an_issues_delivery(self):
        body = {"action": "labeled", "issue": {"number": 12, "user": {"login": "owner"}},
                "label": {"name": "fix"}, "sender": {"login": "maint"}}
        self.assertEqual(trace.facts("issues", body),
                         ["issues/labeled · issue #12 · sender maint · author owner · label fix"])


if __name__ == "__main__":
    unittest.main()
