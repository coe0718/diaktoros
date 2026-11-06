"""#1790: each model request of a turn, traced by the inference proxy: what it kept of the one
before it, and what the provider's cache served. Driven through a real proxy socket and a stub
upstream, written through ``Supervisor.record_usage`` and read back by ``stats``.
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
from diaktoros import request_trace, stats, trusted_turn  # noqa: E402
from diaktoros.inference_proxy import InferenceCapability, _UnixHTTP  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
TOOLS = [{"type": "function", "name": "read_file"}]
ASK = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "review"}]}
THOUGHT = {"type": "reasoning", "encrypted_content": "first"}
CALL = {"type": "function_call", "name": "read_file", "arguments": "{}", "call_id": "c1"}
RESULT = {"type": "function_call_output", "call_id": "c1", "output": "text"}


def responses(*items, instructions="be careful"):
    return json.dumps({"model": "m", "instructions": instructions, "tools": TOOLS,
                       "input": list(items)}).encode()


# A turn of four requests: the first; one that appends to it; one that rewrote the reasoning
# item the previous request carried; and one sent with other instructions.
TURN = [responses(ASK),
        responses(ASK, THOUGHT, CALL, RESULT),
        responses(ASK, {**THOUGHT, "encrypted_content": "rewritten"}, CALL, RESULT, CALL),
        responses(ASK, instructions="be quick")]
USAGE = [(1000, 0), (1500, 900), (1600, 1000), (1700, None)]


def anthropic(*messages, system="be careful"):
    return json.dumps({"model": "m", "system": system, "tools": TOOLS, "max_tokens": 100,
                       "messages": list(messages)}).encode()


# The modes' paths (the proxy's own, then the upstream's) and how each reports its usage.
MODES = {
    "codex_responses": ("/v1/responses", "/v1/responses", lambda t_in, cached: {
        "input_tokens": t_in, "output_tokens": 5,
        **({"input_tokens_details": {"cached_tokens": cached}} if cached is not None else {})}),
    # Anthropic's input_tokens leaves its cache out; the proxy adds it back (#1336).
    "anthropic_messages": ("/anthropic/v1/messages", "/v1/messages", lambda t_in, cached: {
        "input_tokens": t_in - (cached or 0), "output_tokens": 5,
        **({"cache_read_input_tokens": cached} if cached is not None else {})}),
}


def run_turn(bodies, replies, mode="codex_responses", **options):
    """Send ``bodies`` through a real proxy whose upstream answers with ``replies``
    (input tokens, cached tokens or None: not reported); the capability, once all are read.
    ``options`` go to the capability (``quota``, ``nudge_steps``)."""
    local, remote, usage_of = MODES[mode]
    served = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            t_in, cached = replies[len(served)]
            served.append(1)
            data = json.dumps({"output": [], "usage": usage_of(t_in, cached)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d, \
                InferenceCapability(Path(d) / "cap",
                                    f"http://127.0.0.1:{upstream.server_port}{remote}",
                                    "DUMMY_KEY", model="m",
                                    **{"quota": len(bodies), **options}, api_mode=mode) as cap:
            for body in bodies:
                conn = _UnixHTTP(str(cap.socket_path))
                conn.request("POST", local, body=body)
                conn.getresponse().read()
                conn.close()
            for _ in range(100):                          # the handler records after relaying
                if len(served) == len(cap.trace.entries) and all(
                        "in" in entry for entry in cap.trace.entries):
                    break
                threading.Event().wait(0.02)
            return cap
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join()


class Trace(unittest.TestCase):
    def test_each_request_says_what_it_kept_of_the_one_before_and_what_was_cached(self):
        cap = run_turn(TURN, USAGE)
        self.assertEqual(trusted_turn.usage_of(cap)["requests"], [
            {"items": 2, "kept": None, "of": None, "in": 1000, "cached": 0},
            {"items": 5, "kept": 2, "of": 2, "in": 1500, "cached": 900},
            {"items": 6, "kept": 2, "of": 5, "changed": "reasoning", "in": 1600, "cached": 1000},
            {"items": 2, "kept": 0, "of": 6, "changed": "instructions/tools", "in": 1700,
             "cached": None}])

    def test_an_anthropic_turn_counts_its_system_and_tools_and_its_messages(self):
        # Review of #1813, F1: the system prompt is the request's first item there, not a message.
        user = {"role": "user", "content": "review"}
        said = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1",
                                                   "name": "read_file", "input": {}}]}
        got = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                             "content": "text"}]}
        cap = run_turn([anthropic(user), anthropic(user, said, got),
                        anthropic(user, said, got, system="be quick")],
                       [(1000, 0), (1500, 900), (1600, None)], mode="anthropic_messages")
        self.assertEqual(cap.trace.entries, [
            {"items": 2, "kept": None, "of": None, "in": 1000, "cached": 0},
            {"items": 4, "kept": 2, "of": 2, "in": 1500, "cached": 900},
            {"items": 4, "kept": 0, "of": 4, "changed": "instructions/tools", "in": 1600,
             "cached": None}])

    def test_the_nudged_request_is_traced_as_sent_and_a_refused_one_not_at_all(self):
        # Review of #1813 (#1815): the host's nudge is in what the provider saw. The next request
        # carries it again at its own end (#1789), so the history before it changed at the
        # agent's new item. A request past the turn's quota is never sent and gets no entry.
        cap = run_turn([responses(ASK), responses(ASK, CALL), responses(ASK, CALL, RESULT)],
                       [(10, 0), (20, 10)], quota=2, nudge_steps=6)   # nudged: the first request
        self.assertEqual(len(cap.trace.entries), 2)
        nudged, after = cap.trace.entries
        self.assertEqual(nudged["items"], 3)                 # the instructions, ASK, the nudge
        self.assertEqual((after["kept"], after["of"], after["changed"]), (2, 3, "function_call"))
        self.assertEqual(after["items"], 4)                  # ... CALL, then the nudge again

    def test_a_shorter_copy_of_the_previous_request_is_a_drop(self):
        cap = run_turn([responses(ASK, CALL), responses(ASK)], [(10, 0), (10, 0)])
        self.assertEqual(cap.trace.entries[1]["changed"], "dropped")

    def test_an_item_resent_with_its_keys_reordered_is_a_change(self):
        # The provider caches the text it is sent: the same item in another key order is not
        # the same prefix.
        trace = request_trace.Trace("codex_responses")
        trace.see(responses(ASK, CALL))
        reordered = dict(reversed(list(CALL.items())))
        entry = trace.see(responses(ASK, reordered, RESULT))
        self.assertEqual((entry["kept"], entry["of"], entry["changed"]), (2, 3, "function_call"))

    def test_a_request_it_cannot_read_gets_no_entry_and_does_not_break_the_next(self):
        trace = request_trace.Trace("codex_responses")
        self.assertIsNone(trace.see(b"not json"))
        first = trace.see(responses(ASK))
        self.assertEqual(first["kept"], None)
        trace.usage(None, 5, 5)                          # a reply with no entry adds nothing
        self.assertEqual(trace.entries, [first])

    def test_stats_tells_a_kept_history_the_cache_missed_from_one_rewritten(self):
        cap = run_turn(TURN, USAGE)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            sup = Supervisor(db)
            for n, seat in enumerate(("reviewer", "shadow")):
                sup.enqueue(f"d{n}", REPO, 7, "a" * 40, seat)
                with closing(sqlite3.connect(db)) as con, con:
                    con.execute("UPDATE runs SET owner='w', created=? WHERE delivery=?",
                                (1000.0 + n, f"d{n}"))
                    run_id = con.execute("SELECT id FROM runs WHERE delivery=?",
                                         (f"d{n}",)).fetchone()[0]
                observed = trusted_turn.usage_of(cap) if seat == "reviewer" else \
                    {"tokens_in": 5, "tokens_out": 1, "cache_read": None, "cost": None,
                     "requests": []}
                sup.record_usage(run_id, "w", observed)
            # Of the one request that kept the history, the cache served at most 900 of the 1,000
            # tokens it resent; the other two changed it, at a reasoning item and at the instructions.
            # The shadow traced nothing (an empty trace is not recorded) and is left out.
            lines = stats.request_cache(db, REPO, 0)
            self.assertEqual(lines, [
                "reviewer: 4 requests in 1 turns; 1 kept the previous request's history (of "
                "the 1,000 tokens they resent, the cache served at most 900, 90%: a provider "
                "says how many tokens it served, not which); 2 did not (first change: reasoning "
                "×1, instructions/tools ×1)"])
            report = {"repo": REPO, "loop": "w", "since": 0, "until": 86400, "turns": {},
                      "request_cache": lines}
            self.assertIn(lines[0], stats.text(report))

    def test_the_cache_is_never_said_to_serve_more_than_was_resent(self):
        # #1814: a cache can also hold part of what a request appended; 180 of 100 is not a rate.
        turn = [{"kept": None, "of": None, "in": 100},
                {"kept": 2, "of": 2, "in": 200, "cached": 180}]
        # Nor is the capped figure exact (review of #1813, F3): a provider says how many tokens
        # it served, not which, so 80 resent and 100 appended could make the same 180.
        line = request_trace.line("reviewer", request_trace.summary([turn]))
        self.assertIn("of the 100 tokens they resent, the cache served at most 100, 100%: a "
                      "provider says how many tokens it served, not which", line)

    def test_an_item_type_the_sandbox_chose_is_named_other_not_published(self):
        # #1829: through a real proxy, where the type is the sandbox's own text.
        secret = {"type": "worker-prompt-text-snippet", "x": 1}
        cap = run_turn([responses(ASK, CALL), responses(ASK, secret), responses(ASK, CALL)],
                       [(10, 0), (10, 0), (10, 0)])
        self.assertEqual([e.get("changed") for e in cap.trace.entries],
                         [None, "other", "function_call"])
        role = {"type": "message", "role": "worker-prompt-text-snippet"}
        cap = run_turn([responses(ASK, CALL), responses(ASK, role)], [(10, 0), (10, 0)])
        self.assertEqual(cap.trace.entries[1]["changed"], "other")

    def test_a_kind_recorded_before_the_vocabulary_is_not_published_either(self):
        # #1829: stats reads what the ledger holds; a request item that is not an object is
        # named by its JSON type, which is in the vocabulary.
        turn = [{"kept": None, "of": None},
                {"kept": 1, "of": 2, "changed": "worker-prompt-text-snippet"},
                {"kept": 1, "of": 2, "changed": "function_call"}]
        line = request_trace.line("reviewer", request_trace.summary([turn]))
        self.assertNotIn("worker-prompt", line)
        self.assertIn("first change: other ×1, function_call ×1", line)
        trace = request_trace.Trace("codex_responses")
        trace.see(responses(ASK, CALL))
        self.assertEqual(trace.see(responses(ASK, "a string"))["changed"], "str")

    def test_collect_reports_it_in_json_and_the_page_escapes_it(self):
        # Review of #1813, F2: through the ledger and stats.collect, then the JSON and the page.
        # A recorded kind is never published as text: unknown ones are ``other`` (#1829).
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            sup = Supervisor(db)
            sup.enqueue("d", REPO, 7, "a" * 40, "reviewer")
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE runs SET owner='w', created=1000 WHERE delivery='d'")
                run_id = con.execute("SELECT id FROM runs WHERE delivery='d'").fetchone()[0]
            sup.record_usage(run_id, "w", {
                "tokens_in": 30, "tokens_out": 2, "cache_read": 10, "cost": None, "requests": [
                    {"items": 2, "kept": None, "of": None, "in": 10, "cached": 0},
                    {"items": 2, "kept": 1, "of": 2, "changed": "<b>odd</b>", "in": 20,
                     "cached": 10}]})
            report = stats.collect({"repo": REPO, "id": "w"}, db, 0, False, now=86400)
        (line,) = report["request_cache"]
        self.assertIn("first change: other ×1", line)
        self.assertNotIn("odd", line)
        self.assertEqual(json.loads(json.dumps(report))["request_cache"], [line])
        page = stats.as_html(report)
        self.assertIn("first change: other ×1", page)
        self.assertNotIn("odd", page)

    def test_a_ledger_without_traces_reports_none(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            Supervisor(db).enqueue("d", REPO, 7, "a" * 40, "reviewer")
            self.assertIsNone(stats.request_cache(db, REPO, 0))
            self.assertIsNone(stats.request_cache(Path(d) / "missing.sqlite", REPO, 0))


if __name__ == "__main__":
    unittest.main()
