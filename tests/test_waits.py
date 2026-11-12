"""#2080: each time a run is parked in ``waiting``, one line in the worker log says when, which run
(repo, PR, short head, seat, id), until when, and why; a CI hold re-polled with the same reason
adds none. ``hermes dk history`` shows the lines as the PR's waits.

Each path the issue names is driven through its real entry point: the claim's light CI poll and a
claim read that fails (``_claim``, as ``test_ci_poll`` drives it), and a production turn that is
paced or backs off (``_run_production``, as ``test_pacing`` drives it).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import calendar
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ci_poll as cp  # noqa: E402
import test_pacing as tp  # noqa: E402
import test_seat_models as sm  # noqa: E402
from diaktoros import history, history_render  # noqa: E402
from diaktoros.run_supervisor import CI_HOLD, CLAIM_READ_FAILED, Supervisor, waits  # noqa: E402


def waits_in(path: Path) -> list[dict]:
    return [w for w in map(waits._parse, path.read_text().splitlines()) if w]


class ClaimWaits(sm.Base):
    # test_ci_poll's helpers, borrowed so its own tests do not run twice.
    fake_api, hold, claim, row = (cp.LightPoll.fake_api, cp.LightPoll.hold, cp.LightPoll.claim,
                                  cp.LightPoll.row)

    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "review_after_ci": True, "base": "main",
                     "triage": {"maintainers": ["boss"]}}
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps(self.settings))
        runtime.chmod(0o600)
        self.sup = Supervisor(self.root / "ledger.sqlite", production_config=runtime,
                              hermes_home=self.home)
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue("d-2080", "acme/widgets", 7, cp.HEAD, "reviewer")
        self.checks, self.pr, self.calls = cp.RUNNING, cp.pull(), []
        self.log = waits.log_path(self.sup.db)
        self.log.write_text("worker exited: an older line with no time\n")   # the host made it

    def test_a_ci_hold_logs_once_until_its_reason_changes(self):
        self.hold()
        before = time.time()
        self.claim()
        self.assertEqual(self.row()[0], "waiting")
        [line] = waits_in(self.log)
        self.assertEqual((line["repo"], line["pr"], line["head"], line["seat"], line["retries"]),
                         ("acme/widgets", "7", cp.HEAD[:7], "reviewer", "0"))
        self.assertTrue(line["reason"].startswith(CI_HOLD), line)
        self.assertGreaterEqual(calendar.timegm(time.strptime(line["at"], "%Y-%m-%dT%H:%M:%SZ")),
                                int(before) - 1)
        self.assertRegex(line["retry_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        for _ in range(2):                                 # re-polled, same reason: no new line
            self.hold()
            self.claim()
        self.assertEqual(len(waits_in(self.log)), 1)
        self.pr = None                                     # the claim read fails: a new reason
        self.hold()
        self.claim()
        lines = waits_in(self.log)
        self.assertEqual((self.row()[0], len(lines)), ("waiting", 2))
        self.assertTrue(lines[1]["reason"].startswith(CLAIM_READ_FAILED), lines[1])
        self.assertEqual(lines[1]["retries"], "1")

    def test_a_rotated_log_still_remembers_the_last_wait(self):
        """#2185: the host rotates the log past its cap; an unchanged hold is not logged again."""
        self.hold()
        self.claim()
        rotated = self.log.with_name(self.log.name + ".1")
        self.log.rename(rotated)
        self.log.write_text("worker stderr\n" * 8000)        # chatty, and well past 64 KiB
        self.hold()
        self.claim()
        self.assertEqual((len(waits_in(rotated)), len(waits_in(self.log))), (1, 0))

    def test_a_wait_is_not_appended_past_the_log_cap(self):
        """#2302: a worker's wait line never grows the log past WORKER_LOG_MAX, the cap a worker's
        stderr keeps too (``waits.within_cap``); the host's next spawn rotates it."""
        from diaktoros import run_supervisor
        self.log.write_text("x" * (run_supervisor.WORKER_LOG_MAX + 1))
        self.hold()
        self.claim()
        self.assertEqual(self.row()[0], "waiting")
        self.assertEqual(self.log.stat().st_size, run_supervisor.WORKER_LOG_MAX + 1)
        self.log.write_text("worker stderr\n")             # rotated by the host: room again
        self.pr = None                                     # a new reason, so a new line is due
        self.hold()
        self.claim()
        self.assertEqual(len(waits_in(self.log)), 1)


class TurnWaits(tp.ProductionBase):
    def log(self) -> Path:
        path = waits.log_path(self.home / "state" / "diaktoros-runs.sqlite")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_a_paced_turn_logs_its_hold_and_when_it_resumes(self):
        log, reset = self.log(), time.time() + 3600

        def rate_limited(_loop, scope, observed=None, **kw):
            observed.update(rate_limited_until=reset)
            return 1
        row = self.run_production(rate_limited)
        [line] = waits_in(log)
        self.assertEqual((line["pr"], line["seat"], line["run"]), ("8", "fixer", str(row["id"])))
        self.assertTrue(line["reason"].startswith("held: fixer usage window"), line)
        self.assertEqual(line["retry_at"], time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime(row["retry_at"])))

    def test_a_failed_turn_s_backoff_is_logged_each_retry(self):
        log = self.log()
        for retry in (1, 2):
            row = self.run_production(lambda *a, **k: 1, launches=False)
            self.assertEqual((row["state"], row["retries"]), ("waiting", retry))
        lines = waits_in(log)
        self.assertEqual([(w["retries"], w["reason"]) for w in lines],
                         [("1", "turn exited with status 1"), ("2", "turn exited with status 1")])

    def test_history_shows_the_waits_in_their_round(self):
        log = self.log()
        row = self.run_production(lambda *a, **k: 1, launches=False)
        log.write_text(log.read_text() + "2026-01-01T00:00:00Z wait repo=acme/widgets pr=9 "
                       "head=bbbbbbb seat=fixer run=x retries=0 retry_at=- reason=another PR\n")
        data = history.collect(self.loop, self.home / "state" / "diaktoros-runs.sqlite", 8,
                               True, str)
        [rnd] = data["rounds"]
        self.assertEqual(rnd["head"], tp.HEAD)
        [wait] = [e for e in rnd["events"] if e["kind"] == "wait"]
        until = time.strftime("%H:%M", time.gmtime(row["retry_at"]))
        self.assertEqual(wait["text"], f"waiting (retry 1) until {until}: turn exited with "
                                       "status 1")
        self.assertIn(f"fixer waiting (retry 1) until {until}", history_render.text(data))
        self.assertNotIn("another PR", history_render.text(data))
        self.assertEqual(waits.read(self.home / "absent.sqlite", "acme/widgets", 8), [])


class LogMark(unittest.TestCase):
    """#2388 review: ``_spawn``'s mark names the worker log a stderr holds, under either spelling
    (``envnames.get``), so a log rotated since the spawn is still known by its mark rather than by
    the file now at its path."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.db = root / "runs.sqlite"
        held = root / "held.log"                       # what the worker's stderr still holds
        held.write_text("")
        waits.log_path(self.db).write_text("")         # the log at its path now, after rotation
        self.held = os.stat(held)
        self.mark = f"{self.held.st_dev}:{self.held.st_ino}"
        clear = mock.patch.dict(os.environ)
        clear.start()
        self.addCleanup(clear.stop)
        for name in ("DIAKTOROS_WORKER_LOG", "REVIEW_LOOP_WORKER_LOG"):
            os.environ.pop(name, None)

    def known(self, **env):
        with mock.patch.dict(os.environ, env):
            return waits._is_the_log(self.held, self.db)

    def test_either_spelling_of_the_mark_names_the_held_log_and_the_new_one_wins(self):
        self.assertFalse(self.known())                 # no mark: the file at the path decides
        self.assertTrue(self.known(DIAKTOROS_WORKER_LOG=self.mark))
        self.assertTrue(self.known(REVIEW_LOOP_WORKER_LOG=self.mark))
        self.assertFalse(self.known(REVIEW_LOOP_WORKER_LOG="1:2"))
        self.assertFalse(self.known(DIAKTOROS_WORKER_LOG="1:2", REVIEW_LOOP_WORKER_LOG=self.mark))



if __name__ == "__main__":
    unittest.main()
