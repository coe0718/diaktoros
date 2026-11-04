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
from diaktoros import config, request_trace, show, stats, trusted_turn  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
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

    def test_anthropic_prompt_cache_counts_as_input(self):
        # #1336: Anthropic's input_tokens leaves out cache reads and writes, which on an agent turn
        # are nearly all of the input; a daily token cap that ignored them would never fire.
        cached = "".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in (
                {"type": "message_start", "message": {"usage": {
                    "input_tokens": 12, "cache_read_input_tokens": 180000,
                    "cache_creation_input_tokens": 4000, "output_tokens": 1}}},
                {"type": "message_delta", "usage": {"output_tokens": 300}})).encode()
        meter = UsageMeter()
        for i in range(0, len(cached), 11):
            meter.feed(cached[i:i + 11], True)
        meter.finish()
        self.assertEqual((meter.tokens_in, meter.tokens_out), (184012, 300))
        # A later event that repeats the usage must not count the cache twice.
        meter = UsageMeter()
        meter.feed(b'{"usage": {"input_tokens": 12, "cache_read_input_tokens": 100}}', False)
        meter.finish()
        self.assertEqual(meter.tokens_in, 112)
        # OpenAI-style prompt_tokens already include their cached part: nothing is added.
        meter = UsageMeter()
        meter.feed(b'{"usage": {"prompt_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40},'
                   b' "cache_read_input_tokens": 999}}', False)
        meter.finish()
        self.assertEqual(meter.tokens_in, 50)

    def test_cached_input_is_kept_apart_and_unreported_is_unknown(self):
        """#1649: 12 uncached + 100 cache-read and 112 uncached both total 112 in; the meter
        tells them apart, and a reply that reports no cache field is None, not 0."""
        def meter(body):
            m = UsageMeter()
            m.feed(body, False)
            m.finish()
            return m
        cached = meter(b'{"usage": {"input_tokens": 12, "cache_read_input_tokens": 100}}')
        plain = meter(b'{"usage": {"input_tokens": 112}}')
        self.assertEqual((cached.tokens_in, cached.cache_read), (112, 100))
        self.assertEqual((plain.tokens_in, plain.cache_read), (112, None))
        zero = meter(b'{"usage": {"input_tokens": 112, "cache_read_input_tokens": 0}}')
        self.assertEqual(zero.cache_read, 0)
        chat = meter(b'{"usage": {"prompt_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40}}}')
        self.assertEqual((chat.tokens_in, chat.cache_read), (50, 40))

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
                                 {"tokens_in": 320, "tokens_out": 85, "cache_read": None,
                                  "cost": 0.5, "requests": [
                                      {"items": 1, "kept": None, "of": None, "in": 120,
                                       "cached": None},
                                      {"items": 1, "kept": 1, "of": 1, "in": 200,
                                       "cached": None}]})
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_no_reported_cost_is_unknown_not_zero(self):
        class Bare:
            tokens_in, tokens_out, cost, cost_known = 5, 6, 0.0, False
        self.assertIsNone(trusted_turn.usage_of(Bare)["cost"])


class CacheShare(unittest.TestCase):
    def test_share_is_over_reporting_turns_and_unknown_without_any(self):
        self.assertEqual(stats.cache_share({"cache_read": 100, "cached_of": 400}), "100 of 400 (25%)")
        self.assertEqual(stats.cache_share({"cache_read": 0, "cached_of": 0}), "unknown")


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
            self.assertIn(key, settings_form.SETTINGS_SCHEMA)
            self.assertIn(f"  {key}:", text)
            self.assertIn(settings_form.SETTINGS_SCHEMA[key]["description"], text.replace('\\"', '"'))


class CachePerRound(unittest.TestCase):
    """#1649: what the provider's cache served is kept for each turn, written through
    ``Supervisor.record_usage``, read back per round by ``stats``, with an unreporting turn
    unknown and never 0."""
    REPO = "acme/widgets"

    def test_two_rounds_are_told_apart_through_the_ledger(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            sup = Supervisor(db)
            for n, (head, observed) in enumerate((
                    ("a" * 40, {"tokens_in": 100, "tokens_out": 1, "cache_read": 0, "cost": None}),
                    ("b" * 40, {"tokens_in": 100, "tokens_out": 1, "cache_read": 100, "cost": None}),
                    ("c" * 40, {"tokens_in": 100, "tokens_out": 1, "cache_read": None,
                                "cost": None}))):
                sup.enqueue(f"d{n}", self.REPO, 7, head, "reviewer")
                with closing(sqlite3.connect(db)) as con, con:
                    con.execute("UPDATE runs SET owner='w', created=? WHERE delivery=?",
                                (1000.0 + n, f"d{n}"))
                    run_id = con.execute("SELECT id FROM runs WHERE delivery=?",
                                         (f"d{n}",)).fetchone()[0]
                sup.record_usage(run_id, "w", observed)
            rounds = stats.cache_by_turn(db, self.REPO, 0)
            self.assertEqual([r["cache_read"] for r in rounds], [0, 100, None])
            self.assertEqual([r["head"] for r in rounds], ["aaaaaaa", "bbbbbbb", "ccccccc"])
            rows = stats._cache_turn_rows({"cache_turns": rounds})
            self.assertEqual([r[4] for r in rows],
                             ["0 of 100 (0%)", "100 of 100 (100%)", "unknown"])
            # The per-seat sum covers only the reporting turns: 100 of 200, not of 300.
            (total,) = stats.usage(db, self.REPO, 0)
            self.assertEqual((total["cache_read"], total["cached_of"]), (100, 200))
            self.assertIn("100 of 200 (50%)", stats.cache_share(total))
            report = {"repo": self.REPO, "loop": "w", "since": 0, "until": 86400, "turns": {},
                      "usage": [total], "cache_turns": rounds}
            self.assertIn("Prompt cache per turn", stats.text(report))

    def test_a_reply_that_reports_no_cache_makes_the_turn_unknown(self):
        """A turn of two requests, one reporting and one not, is not "100 of 300": unknown."""
        class Cap(InferenceCapability):
            def __init__(self):                              # the totals only, no socket
                self.lock = threading.Lock()
                self.tokens_in = self.tokens_out = 0
                self.cache_read, self.cache_gap = None, False
                self.cost, self.cost_known = 0.0, False
                self.trace = request_trace.Trace("chat_completions")

        def meter(body):
            m = UsageMeter()
            m.feed(body, False)
            m.finish()
            return m
        reporting = b'{"usage": {"input_tokens": 12, "cache_read_input_tokens": 88}}'
        silent = b'{"usage": {"input_tokens": 200}}'
        for order in ((reporting, silent), (silent, reporting)):
            cap = Cap()
            for body in order:
                cap.add_usage(meter(body))
            self.assertEqual(cap.tokens_in, 300)
            self.assertIsNone(cap.cache_read)
            self.assertIsNone(trusted_turn.usage_of(cap)["cache_read"])
        cap = Cap()
        cap.add_usage(meter(reporting))
        cap.add_usage(meter(reporting))
        self.assertEqual(trusted_turn.usage_of(cap)["cache_read"], 176)
