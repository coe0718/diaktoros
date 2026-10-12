"""#480: tokens and cost per turn, `stats` sums per seat and model, `daily_tokens` validation.

The worker-side budget hold is in test_pacing (it needs that file's production harness).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import config, show, stats, trusted_turn  # noqa: E402
from diaktoros.inference_proxy import PATH, InferenceCapability, UsageMeter, _UnixHTTP  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

CHAT = json.dumps({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                                            "cost": 0.5}}).encode()
ANTHROPIC = "".join(
    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in (
        {"type": "message_start", "message": {"usage": {"input_tokens": 200, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"text": "hi"}},
        {"type": "message_delta", "usage": {"output_tokens": 55}})).encode()


class Meter(unittest.TestCase):
    def test_buffered_and_streamed_usage(self):
        for body, stream, want in ((CHAT, False, (120, 30, 0.5)),
                                   (ANTHROPIC, True, (200, 55, 0.0))):
            meter = UsageMeter()
            for i in range(0, len(body), 7):                 # arbitrary chunking
                meter.feed(body[i:i + 7], stream)
            meter.finish()
            self.assertEqual((meter.tokens_in, meter.tokens_out, meter.cost), want)
        self.assertFalse(meter.cost_known)

    def test_reported_zero_cost_is_known(self):
        for body, stream in ((b'{"usage": {"prompt_tokens": 5, "cost": 0}}', False),
                             (b'data: {"usage": {"output_tokens": 3, "cost": 0.0}}\n', True)):
            meter = UsageMeter()
            meter.feed(body, stream)
            meter.finish()
            self.assertTrue(meter.cost_known)
            self.assertEqual(meter.cost, 0.0)
        meter = UsageMeter()
        meter.feed(b'{"usage": {"prompt_tokens": 5}}', False)
        meter.finish()
        self.assertFalse(meter.cost_known)

    def test_junk_counts_as_nothing(self):
        meter = UsageMeter()
        meter.feed(b'{"usage": {"prompt_tokens": true, "completion_tokens": "9"}}', False)
        meter.finish()
        self.assertEqual((meter.tokens_in, meter.tokens_out), (0, 0))


class ProxyRecordsTokens(unittest.TestCase):
    def test_every_reply_adds_to_the_turn(self):
        replies = [(CHAT, "application/json"), (ANTHROPIC, "text/event-stream")]
        served = []

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                data, label = replies[len(served)]
                served.append(1)
                self.send_response(200)
                self.send_header("Content-Type", label)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d, \
                    InferenceCapability(Path(d) / "cap",
                                        f"http://127.0.0.1:{upstream.server_port}{PATH}",
                                        "DUMMY_KEY", model="m", quota=2) as cap:
                for _ in replies:
                    conn = _UnixHTTP(str(cap.socket_path))
                    conn.request("POST", PATH, body=b'{"messages": []}')
                    conn.getresponse().read()
                    conn.close()
                for _ in range(100):                          # the handler records after relaying
                    if cap.tokens_in == 320:
                        break
                    threading.Event().wait(0.02)
                self.assertEqual((cap.tokens_in, cap.tokens_out), (320, 85))
                self.assertTrue(cap.cost_known)
                self.assertEqual(trusted_turn.usage_of(cap),
                                 {"tokens_in": 320, "tokens_out": 85, "cost": 0.5})
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_no_reported_cost_is_unknown_not_zero(self):
        class Bare:
            tokens_in, tokens_out, cost, cost_known = 5, 6, 0.0, False
        self.assertIsNone(trusted_turn.usage_of(Bare)["cost"])


class StatsSumsPerSeatAndModel(unittest.TestCase):
    def test_usage_by_seat_and_model(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            Supervisor(db, create=True) if "create" in Supervisor.__init__.__code__.co_varnames \
                else Supervisor(db)
            with closing(sqlite3.connect(db)) as con, con:
                cols = [r[1] for r in con.execute("PRAGMA table_info(runs)")]
                self.assertTrue({"tokens_in", "tokens_out", "cost"} <= set(cols))
                base = {c: None for c in cols}
                notnull = {r[1]: r[4] for r in con.execute("PRAGMA table_info(runs)") if r[3]}
                for n, (seat, model, t_in, t_out, cost) in enumerate((
                        ("reviewer", "m1", 100, 10, 0.5), ("reviewer", "m1", 50, 5, None),
                        ("reviewer", "m2", 7, 1, None), ("fixer", "m1", 9, 9, None),
                        ("fixer", "m1", None, None, None))):
                    row = {**base, "id": f"r{n}", "repo": "acme/widgets", "seat": seat,
                           "pr": n, "head": f"{n:040d}", "delivery": f"d{n}", "model": model,
                           "created": 1000.0 + n, "state": "succeeded",
                           "tokens_in": t_in, "tokens_out": t_out, "cost": cost}
                    for c, default in notnull.items():
                        if row.get(c) is None:
                            row[c] = (default or "0").strip("'") if default else ""
                    con.execute(f"INSERT INTO runs ({','.join(row)}) VALUES "
                                f"({','.join('?' * len(row))})", list(row.values()))
            got = {(u["seat"], u["model"]): u for u in stats.usage(db, "acme/widgets", 0)}
            self.assertEqual((got[("reviewer", "m1")]["tokens_in"],
                              got[("reviewer", "m1")]["tokens_out"],
                              got[("reviewer", "m1")]["cost"], got[("reviewer", "m1")]["costed"]),
                             (150, 15, 0.5, 1))
            self.assertEqual(got[("reviewer", "m2")]["costed"], 0)
            self.assertEqual(got[("fixer", "m1")]["turns"], 1, "a turn with no tokens is left out")
            report = {"repo": "acme/widgets", "loop": "w", "since": 0, "until": 86400,
                      "turns": {}, "usage": list(got.values())}
            self.assertIn("unknown", "\n".join(" ".join(r) for r in stats._usage_rows(report)))
            self.assertIn("Tokens per seat and model", stats.text(report))


class DailyTokensSetting(unittest.TestCase):
    RAW = {"id": "w", "repo": "acme/widgets", "fixers": ["f"], "reviewers": ["r"],
           "read_token": "rd", "seats": {"reviewer": {"profile": "p", "route": "r"},
                                         "fixer": {"profile": "q", "route": "f"}}}

    def loop(self, **seat):
        return config.normalize({**self.RAW, "seats": {
            **self.RAW["seats"], "fixer": {**self.RAW["seats"]["fixer"], **seat}}})

    def test_accepts_and_refuses(self):
        self.assertEqual(config.seat_daily_tokens(self.loop(daily_tokens=5000), "fixer"), 5000)
        self.assertEqual(config.seat_daily_tokens(self.loop(daily_tokens=5000), "issue_fixer"), 5000)
        self.assertIsNone(config.seat_daily_tokens(self.loop(), "fixer"))
        for bad in (0, -1, True, "5", 1.5, config.DAILY_TOKENS_MAX + 1):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                self.loop(daily_tokens=bad)

    def test_settings_form_schema_and_plugin_yaml_agree(self):
        text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
        for key in ("reviewer_daily_tokens", "fixer_daily_tokens"):
            self.assertIn(key, config.SETTINGS_SCHEMA)
            self.assertIn(f"  {key}:", text)
            self.assertIn(config.SETTINGS_SCHEMA[key]["description"], text.replace('\\"', '"'))


if __name__ == "__main__":
    unittest.main()
