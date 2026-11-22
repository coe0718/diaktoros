"""#2303 (#2589 round 3): junk entries in a keyed state file read as absent, so the real callers
that walk the readers' entries neither raise nor lose the valid entries beside the junk.

The walk (``test_state_junk_all``) found ``breach_all``, ``findings_all``, ``override_checked``,
``queue_all`` and ``watch`` handing back an object of junk as stored; each caller here crashed
on it: ``status`` on ``breach.json``, a confirmed review on ``pending.json``, the shadow report
and ``missed_count`` on ``findings.json``, ``transition.record`` on ``watchdog.json``, and
``findings.record`` on a PR entry whose ``findings`` is junk (every later review of that PR).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from argparse import Namespace
from unittest import mock

import test_escalate as escalate_fx
import test_shadow as shadow_fx
from diaktoros import config, findings, stale_approval, state, stats, transition
from diaktoros import one_shot_action as actions
from diaktoros.cli import operations
import _state_walk as walk

ROOT = Path(__file__).resolve().parents[1]
HEAD, OTHER = "a" * 40, "b" * 40


def state_in(root: Path) -> state.LoopState:
    st = state.LoopState(walk.new_loop(root))
    st.dir.mkdir(parents=True, exist_ok=True)
    return st


class Callers(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.st = state_in(self.root)

    def write(self, name: str, data) -> None:
        (self.st.dir / name).write_text(json.dumps(data))

    def test_status_lists_the_valid_breach_beside_junk_ones(self):
        self.write("breach.json", {"acme/widgets#7": {"pr": 7, "head": HEAD,
                                                      "status": "awaiting-adjudication"},
                                   "acme/widgets#8": "junk", "acme/widgets#9": {"head": 7}})
        full = config.normalize({
            "id": "junk", "repo": "acme/widgets", "state_dir": str(self.root / "state"),
            "fixers": ["fix"], "reviewers": ["review"], "tokens": {}, "read_token": "read",
            "seats": {"reviewer": {"route": "r", "profile": "critic", "login": "review"},
                      "fixer": {"route": "f", "profile": "coder", "login": "fix"}}})
        out = io.StringIO()
        with mock.patch.object(operations.config, "load_id", return_value=full), \
                mock.patch.object(operations.state_mod, "state_for", return_value=self.st), \
                mock.patch.object(operations, "_print_ledger_runs"), \
                contextlib.redirect_stdout(out):
            operations.cmd_status(Namespace(loop="junk", seat=None, json=False))
        breaches = [line for line in out.getvalue().splitlines() if "breach:" in line]
        self.assertEqual(breaches, ["  breach:     acme/widgets#7 at aaaaaaa — "
                                    "awaiting-adjudication"])

    def test_missed_count_skips_junk_findings(self):
        self.write("findings.json", {
            "7": {"findings": "junk"}, "9": "junk", "x": {"findings": {}},
            "8": {"findings": {"F1": {"missed": True, "at": time.time()}, "F2": "junk"}}})
        self.assertEqual(findings.missed_count(self.st.loop), 1)
        self.assertEqual(self.st.findings_all(), {
            "7": {"findings": {}}, "8": {"findings": {"F1": mock.ANY}}})

    def test_findings_record_writes_over_a_junk_findings_field(self):
        self.write("findings.json", {
            "7": {"findings": {"F1": "junk", "F2": {"state": "open", "text": "kept"}},
                  "last_head": OTHER},
            "8": {"findings": "junk", "last_head": OTHER}})
        findings.record(self.st.loop, 7, HEAD, "F2: fixed\nF3: a.py:1: new")
        findings.record(self.st.loop, 8, HEAD, "F1: b.py:2: new")
        findings.record(self.st.loop, 8, OTHER, "F1: fixed")       # later reviews still record
        seven, eight = self.st.findings_get(7), self.st.findings_get(8)
        self.assertEqual(sorted(seven["findings"]), ["F2", "F3"])
        self.assertEqual(seven["findings"]["F2"]["state"], "fixed")
        self.assertEqual((eight["last_head"], eight["findings"]["F1"]["state"]), (OTHER, "fixed"))

    def test_transition_record_reads_junk_watch_values_as_absent(self):
        self.write("watchdog.json", {"heads": "junk", "stacked_wait": "junk", "last_run": 5,
                                     "armed_since": time.time()})
        with mock.patch.object(transition.gh, "reviews_read", return_value=([], None)):
            self.assertIsNone(transition.record(self.st.loop, self.st, 7, HEAD, "main"))
        # An object a reader indexes into reads as absent; a stamp is kept as stored, junk too,
        # so a sweep that saves the file back unchanged (a failed listing) erases nothing.
        self.assertEqual(sorted(self.st.watch()), ["armed_since", "last_run"])
        self.write("watchdog.json", {"x": {}, "armed_since": [1], "heads": {}})  # x: no writer
        self.assertEqual(self.st.watch(), {"armed_since": [1], "heads": {}})

    def test_override_checked_keeps_only_merged_heads(self):
        self.write(state.LoopState.OVERRIDE_CHECKED_FILE,
                   {"7": HEAD, "8": "junk", "9": 7, "reviewer": HEAD})
        self.assertEqual(self.st.override_checked(), {"7": HEAD})

    def test_every_watchdog_key_the_code_uses_is_kept(self):
        # A key missing from WATCH_KEYS would be dropped on its first read, so check the code.
        used = {stale_approval.KEY}
        for path in [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]:
            used |= set(re.findall(r"\bwatch(?:\[|\.get\(|\.setdefault\(|\.pop\()[\"'](\w+)[\"']",
                                   path.read_text(encoding="utf-8")))
        self.assertGreater(len(used), 8)
        self.assertLessEqual(used, set(state.WATCH_KEYS), sorted(used - set(state.WATCH_KEYS)))


class ConfirmedReview(escalate_fx.Base):
    def test_a_review_reads_a_junk_queue_as_no_queued_request(self):
        key = actions.gate.seat_key(self.loop, 7)
        self.st.dir.mkdir(parents=True, exist_ok=True)
        for queue in ({"reviewer": "junk", "fixer": {"k": {"at": 1.0}}},
                      {"reviewer": {key: "junk"}}):
            with self.subTest(queue=queue):
                self.st.pending.write_text(json.dumps(queue))
                with self.github(), mock.patch.object(actions, "_run_reviewer", return_value=[]):
                    result = actions.perform_review(self.loop, 7)
                self.assertEqual((result["ok"], result["head"]), (False, escalate_fx.HEAD))
        self.st.pending.write_text(json.dumps({"reviewer": "junk", "fixer": {"k": {"at": 1.0}}}))
        self.assertEqual(self.st.queue_all(), {"fixer": {"k": {"at": 1.0}}})
        self.st.pending.write_text(json.dumps({"reviewer": {key: "junk"}}))  # status names it
        self.assertEqual(self.st.queue_all(), {"reviewer": {key: "junk"}})


class ShadowReport(unittest.TestCase):
    setUp = shadow_fx.Ledger.setUp                    # the run ledger, without Ledger's tests
    shadow_at, real_at, add_at = (shadow_fx.Compare.shadow_at, shadow_fx.Compare.real_at,
                                  shadow_fx.Compare.add_at)

    def test_the_report_pairs_shadows_beside_junk_findings_entries(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        st = state_in(Path(temp.name))
        (st.dir / "findings.json").write_text(json.dumps({
            "8": "junk", "9": {"findings": "junk"},
            "10": {"findings": {"F1": {"text": "k.py:2: off by one", "head": OTHER},
                                "F2": "junk"}}}))
        for pr in (8, 9, 10):
            self.shadow_at(pr, OTHER, "APPROVE", "looks fine")
            self.real_at(pr, OTHER, "REQUEST_CHANGES")
        got = stats.report(st.loop, self.db, 0, time.time() + 10, breaches={},
                           pr_state=lambda n: None)["shadows"]
        self.assertEqual([c["pr"] for c in got["approved_blocked"]], [8, 9, 10])
        self.assertEqual(got["approved_blocked"][2]["findings"], ["k.py:2: off by one"])


if __name__ == "__main__":
    unittest.main()
