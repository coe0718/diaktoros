"""#2304 (review F3): every stall or grace decision that consumes ``util.age_min`` raises its
stall on a missing or garbage time, never reads it as "just now".

``age_min`` is None for a missing or unreadable stamp and ``past`` puts None past every grace
(#563). A decision that wrote ``age_min(...) or 0.0`` would turn that into a fresh zero and hide
the stall, and a test of ``age_min`` and ``past`` alone would not notice. So each consumer in
``AGE_DECISIONS`` is driven here through its own branch of ``scripts/watchdog.py``
(``_stall_kind``'s push-off check, ``_fixer_kind``'s fixer-never-pushed check,
``_disputed_kind``'s grace, and
``parked_kind``'s awaiting and adjudicating markers) with the time missing and garbage, and two
controls: a valid old time that raises the stall and a valid fresh one that does not. The scan
fails on an ``age_min`` call not registered here.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("watchdog_age_2304", ROOT / "scripts/watchdog.py")
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

HEAD = "a" * 40
MISSING = object()                                    # the stamp's key is absent
BAD = (MISSING, None, "", "garbage", "2026-13-99T99:99:99Z")
LOOP = {"id": "l", "repo": "o/r", "cap": 3}
GRACE_MIN = 30.0
# path::function -> how many age_min calls it makes; each has a test below.
AGE_DECISIONS = {
    "scripts/watchdog.py::_stall_kind": 1,       # test_push_off_wait
    "scripts/watchdog.py::_fixer_kind": 1,       # test_fixer_never_pushed (via _stall_kind)
    "scripts/watchdog.py::_disputed_kind": 1,    # test_disputed_wait
    "scripts/watchdog.py::parked_kind": 2,       # test_parked_awaiting, test_parked_adjudicating
}


def stamp(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))


OLD, FRESH = stamp(3 * GRACE_MIN), stamp(1)


def with_time(record: dict, key: str, value) -> dict:
    return dict(record) if value is MISSING else {**record, key: value}


def age_min_calls() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted([*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        rel = path.relative_to(ROOT).as_posix()
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            own = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and (
                getattr(n.func, "id", None) == "age_min"
                or getattr(n.func, "attr", None) == "age_min")]
            if own and fn.name != "age_min":
                found[f"{rel}::{fn.name}"] = found.get(f"{rel}::{fn.name}", 0) + len(own)
    return found


class EveryAgeDecision(unittest.TestCase):
    def test_every_age_min_call_is_registered(self):
        self.assertEqual(age_min_calls(), AGE_DECISIONS,
                         "an age_min call was added or moved: give its stall decision a "
                         "missing- and garbage-time test here and register it")

    # -- _stall_kind / _disputed_kind: a changes-requested verdict at this head ------------

    def stall_kind(self, submitted, push_enabled=True, disputed=None):
        latest = with_time({"state": "CHANGES_REQUESTED", "user": {"login": "r"}},
                           "submitted_at", submitted)
        c = SimpleNamespace(loop=LOOP, st=SimpleNamespace(), breach={}, grace={"fixer": GRACE_MIN},
                            current_heads={"7": {"observed_at": None}}, now=time.time())
        gate, config = watchdog.gate, watchdog.config
        views = watchdog.run_supervisor.views
        with mock.patch.object(gate, "changes_at_head", return_value=[latest]), \
                mock.patch.object(gate, "verdicts", return_value=[latest]), \
                mock.patch.object(gate, "breach_holds", return_value=False), \
                mock.patch.object(gate, "reviewer_login", return_value="r"), \
                mock.patch.object(watchdog.gh, "review_state", return_value="CHANGES_REQUESTED"), \
                mock.patch.object(config, "review_only", return_value=set()), \
                mock.patch.object(config, "unattended_fixer_push_enabled",
                                  return_value=push_enabled), \
                mock.patch.object(config, "fixer_push_enable_command", return_value="enable"), \
                mock.patch.object(watchdog.run_supervisor, "production_ledger", return_value="db"), \
                mock.patch.object(views, "disputed", return_value=disputed), \
                mock.patch.object(views, "dispute_choices", return_value="choose"), \
                mock.patch.object(watchdog.watch_health, "live_turn", return_value=False):
            return watchdog._stall_kind(c, {"title": "t"}, 7, HEAD, "someone", [latest], latest,
                                        None)

    def check_decision(self, run, raised: str, says_unreadable=True):
        for bad in BAD:
            kind = run(bad)
            with self.subTest(time=bad if bad is not MISSING else "missing"):
                self.assertIn(raised, kind, "an unknown time hid the stall")
                if says_unreadable:
                    self.assertIn("unreadable", kind, "an unknown time was given an age")
        self.assertIn(raised, run(OLD))                       # control: a real old time
        self.assertNotIn("unreadable", run(OLD))
        self.assertEqual(run(FRESH), "", "a fresh time is within its grace")

    def test_push_off_wait(self):
        self.check_decision(lambda at: self.stall_kind(at, push_enabled=False),
                            watchdog.PUSH_OFF_KIND)

    def test_fixer_never_pushed(self):
        self.check_decision(lambda at: self.stall_kind(at), "fixer never pushed")

    def test_disputed_wait(self):
        # Its wording ages the dispute (a readable time from the ledger), not the verdict.
        disputed = time.time() - 3600
        self.check_decision(lambda at: self.stall_kind(at, disputed=disputed),
                            "pushed nothing; waiting for you", says_unreadable=False)

    # -- parked_kind: a breach marker at this head, no ruling, no live adjudicator run ----

    def parked(self, marker: dict) -> str:
        views = watchdog.run_supervisor.views
        with mock.patch.object(views, "ruling_at", return_value=None), \
                mock.patch.object(views, "turn_state", return_value=None), \
                mock.patch.object(watchdog.watch_mode, "TEST", False), \
                mock.patch.object(watchdog.config, "host_path", return_value="ledger"), \
                mock.patch.object(watchdog.config, "adjudicating_stall_s",
                                  return_value=GRACE_MIN * 60):
            return watchdog.parked_kind(LOOP, marker, 7, HEAD, GRACE_MIN)

    def test_parked_awaiting(self):
        marker = {"head": HEAD, "status": "awaiting-adjudication"}
        self.check_decision(lambda at: self.parked(with_time(marker, "at", at)),
                            "parked awaiting adjudication")

    def test_parked_adjudicating(self):
        marker = {"head": HEAD, "status": "adjudicating"}
        self.check_decision(lambda at: self.parked(with_time(marker, "adjudicating_at", at)),
                            "but no adjudicator run is live")


if __name__ == "__main__":
    unittest.main()
