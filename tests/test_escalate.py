"""#459: `hermes dk escalate --pr N` sends one PR to the adjudicator now, whatever its count.

The escalation writes the breach marker a spent cap writes, marked `escalated`, and from then on
that head is parked as if its cap were spent: the ruling turn passes the worker's pre-launch check
below the cap, both gates start nothing more there, a queued review or fix is retired, the
watchdog retries a pending delivery, and `explain` shows it parked. A new head supersedes it.
Offline: GitHub is mocked; config, state and the run ledger are real, in a temporary home.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import cli, config, gate, gh, ledger, run_supervisor, state as state_mod  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from scripts import gate_fixer, gate_reviewer  # noqa: E402

HEAD = fg.HEAD
OTHER = "c" * 40


def verdict(rid=5, state="CHANGES_REQUESTED", head=HEAD):
    return {"id": rid, "state": state, "commit_id": head,
            "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer"}}


class Base(fg.Base):
    def setUp(self):
        super().setUp()
        self.configure(adjudicator=True)
        self.reviews = [verdict()]
        self.live = dict(fg.LIVE)

    def configure(self, *, adjudicator: bool, push: bool = True):
        path = config.config_dir() / "one.json"
        data = {**fg.raw_loop(push), "state_dir": str(self.root / "state")}
        if adjudicator:
            data["adjudicator"] = {"route": "one-breach"}
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.st = state_mod.state_for(self.loop)

    @contextlib.contextmanager
    def github(self):
        with mock.patch.object(gh, "pr", side_effect=lambda *a, **k: self.live), \
                mock.patch.object(gh, "reviews", side_effect=lambda *a, **k: self.reviews), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake, \
                mock.patch.object(gate.observer, "notify"):
            yield wake

    def escalate(self, reason="talking past each other"):
        with self.github() as wake, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_escalate(argparse.Namespace(loop="one", pr=7, reason=reason))
        return rc, out.getvalue(), wake


class Command(Base):
    def test_an_escalation_parks_the_head_and_wakes_the_adjudicator(self):
        rc, out, wake = self.escalate("they disagree\non scope")
        self.assertEqual(rc, 0, out)
        self.assertIn("escalated at 1/3 verdicts", out)
        wake.assert_called_once()
        marker = self.st.breach_get(7)
        self.assertEqual((marker["head"], marker["rounds"], marker["status"]),
                         (HEAD, 1, "awaiting-adjudication"))
        self.assertEqual(marker["escalated"]["by"], "operator")
        self.assertNotIn("\n", marker["escalated"]["reason"])
        self.assertTrue(gate.escalated(marker, HEAD))
        self.assertFalse(gate.escalated(marker, OTHER))          # a new head supersedes it
        rc, out, _ = self.escalate()
        self.assertEqual(rc, 1)
        self.assertIn("already awaiting a ruling", out)

    def test_refusals(self):
        cases = {
            "no adjudicator": lambda: self.configure(adjudicator=False),
            "approved at this head": lambda: setattr(self, "reviews", [verdict(state="APPROVED")]),
            "no verdict yet": lambda: setattr(self, "reviews", []),
            "only an open, ready PR": lambda: setattr(self, "live", {**fg.LIVE, "draft": True}),
        }
        for want, arrange in cases.items():
            with self.subTest(want=want):
                self.configure(adjudicator=True)
                self.reviews, self.live = [verdict()], dict(fg.LIVE)
                arrange()
                rc, out, wake = self.escalate()
                self.assertEqual(rc, 1, out)
                self.assertIn(want, out)
                wake.assert_not_called()
                self.assertEqual(self.st.breach_get(7), {})

    def test_a_run_in_flight_refuses(self):
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, HEAD, "reviewer")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running'")
        rc, out, wake = self.escalate()
        self.assertEqual(rc, 1)
        self.assertIn("in flight", out)
        wake.assert_not_called()


class Parked(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.escalate()[0], 0)

    def set_marker(self, marker: dict) -> None:
        # breach_set never re-arms a claimed head, by design; a test that needs another marker
        # state writes the loop's breach file as the state module does.
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})

    def row(self, seat="adjudicator"):
        return {"repo": fg.REPO, "pr": 7, "head": HEAD, "seat": seat, "turn_key": "breach:1",
                "id": "r1", "created": time.time()}

    def adjudication(self):
        pr = {**fg.LIVE, "head": {"sha": HEAD, "ref": "fix-7"}}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(run_supervisor, "effective_reviews",
                                  side_effect=lambda loop, row, reviews, db=None: reviews):
            return run_supervisor.adjudication_state(self.loop, self.row())[0]

    def test_the_ruling_runs_below_the_cap_only_when_escalated(self):
        self.assertEqual(self.adjudication(), "ok")
        marker = self.st.breach_get(7)
        self.set_marker({k: v for k, v in marker.items() if k != "escalated"})
        self.assertEqual(self.adjudication(), "superseded")

    def test_the_fixer_gate_starts_no_fix_at_an_escalated_head(self):
        payload = {"action": "submitted", "repository": {"full_name": fg.REPO},
                   "pull_request": fg.LIVE, "review": verdict(6) | {"state": "changes_requested"}}
        with mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_fixer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_fixer.gate, "fetch_reviews", return_value=[verdict(5), verdict(6)]), \
                mock.patch.object(gate_fixer.gate, "drain_seat"), \
                mock.patch.object(gate, "enqueue_isolated") as enqueue, \
                mock.patch.object(gate, "breach") as breach, \
                mock.patch.object(gate_fixer.observer, "notify"):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        enqueue.assert_not_called()
        breach.assert_not_called()
        self.assertIn("escalated to adjudication", err.getvalue())

    def test_the_reviewer_gate_starts_no_review_at_an_escalated_head(self):
        payload = {"action": "review_requested", "number": 7, "pull_request": fg.LIVE,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": "fixer"},
                   "requested_reviewer": {"login": "reviewer"}}
        self.reviews = [verdict(head=OTHER)]                    # earlier round, older head
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_reviewer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=self.reviews), \
                mock.patch.object(gate_reviewer.gate, "block_pr_agent") as block, \
                mock.patch.object(gate_reviewer.observer, "notify"):
            with self.assertRaises(SystemExit):
                gate_reviewer.main()
        block.assert_not_called()
        self.assertIn("escalated to adjudication", err.getvalue())

    def test_a_queued_fix_at_the_escalated_head_is_retired(self):
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("f", fg.REPO, 7, HEAD, "fixer", require_push_admission=True)
        pr = {**fg.LIVE, "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": fg.REPO}}}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=self.reviews):
            self.assertIsNone(sup._claim())
        row = sup.get("f")
        self.assertNotEqual(row["state"], "claimed")
        self.assertIn("escalated to adjudication", str(row["error"] or ""))

    def explain_parked(self, ruled=None):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": self.st.breach_get(7), "parked": True,
                 "delivery_status": "adjudicating",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local), \
                mock.patch.object(gate, "_ruled", return_value=ruled):
            return gate.explain(self.loop, self.st, 7, {"pr": fg.LIVE, "reviews": self.reviews,
                                                        "armed": True, "read_at": time.time()})

    def test_explain_after_the_ruling_says_the_decision_is_yours(self):
        # Live on #537: explain still said the adjudicator rules next after its ruling posted.
        report = self.explain_parked(ruled="REJECT")
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertIn("the adjudicator ruled REJECT", report["next"]["action"])
        self.assertTrue(any("ruled: the adjudicator ruled REJECT" in b for b in report["blockers"]))
        self.assertFalse(any("parked awaiting adjudication" in b for b in report["blockers"]))
        self.assertEqual(self.explain_parked()["next"]["kind"], "adjudication")

    def test_explain_shows_it_parked(self):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": self.st.breach_get(7), "parked": True,
                 "delivery_status": "awaiting-adjudication",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            report = gate.explain(self.loop, self.st, 7, {"pr": fg.LIVE, "reviews": self.reviews,
                                                          "armed": True, "read_at": time.time()})
        self.assertEqual(report["next"]["kind"], "adjudication")

    def test_the_watchdog_retries_a_pending_escalation(self):
        from scripts import watchdog
        marker = self.st.breach_get(7)
        self.set_marker({**marker, "status": "delivery-pending"})
        with mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(gate, "breach") as breach:
            watchdog.retry_pending_breaches(self.loop, self.st, [fg.LIVE])
        breach.assert_called_once()
        self.assertEqual(breach.call_args.kwargs["escalated"]["by"], "operator")


if __name__ == "__main__":
    unittest.main()
