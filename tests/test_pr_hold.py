"""#477: a maintainer's label or comment holds one PR; a held PR starts nothing."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, ledger, pr_hold, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import CONFLICT_KEY, Supervisor  # noqa: E402

BOSS, STRANGER = "boss", "stranger"


def label(who, event="labeled", at="2026-01-01T00:00:00Z", name=pr_hold.LABEL):
    return {"event": event, "actor": {"login": who}, "label": {"name": name}, "created_at": at}


def comment(who, body, at="2026-01-01T00:00:00Z"):
    return {"user": {"login": who}, "body": body, "created_at": at}


class Decide(unittest.TestCase):
    loop = {"triage": {"maintainers": ["Boss"]}}

    def decide(self, events=(), comments=()):
        return pr_hold.decide(self.loop, 7, list(events), list(comments))

    def test_a_maintainers_label_holds_and_records_who_and_when(self):
        hold = self.decide([label("Boss")])
        self.assertEqual((hold["by"], hold["at"], hold["how"]),
                         (BOSS, "2026-01-01T00:00:00Z", "label"))

    def test_a_maintainers_comment_holds_and_release_lifts_it(self):
        self.assertEqual(self.decide(comments=[comment(BOSS, "/loop hold")])["how"], "comment")
        self.assertIsNone(self.decide(comments=[comment(BOSS, "/loop hold"),
                                                comment(BOSS, "/loop release", "2026-01-02T00:00:00Z")]))

    def test_removing_the_label_lifts_a_label_hold(self):
        self.assertIsNone(self.decide([label(BOSS), label(BOSS, "unlabeled", "2026-01-02T00:00:00Z")]))

    def test_a_non_maintainers_hold_is_ignored_and_logged(self):
        with mock.patch.object(pr_hold, "log") as log:
            self.assertIsNone(self.decide([label(STRANGER)],
                                          [comment(STRANGER, "/loop hold")]))
        self.assertEqual(log.call_count, 2)
        self.assertIn("stranger", log.call_args[0][0])

    def test_a_strangers_release_does_not_lift_a_hold(self):
        self.assertIsNotNone(self.decide(comments=[comment(BOSS, "/loop hold"),
                                                   comment(STRANGER, "/loop release",
                                                           "2026-01-02T00:00:00Z")]))


class Claim(fg.Base):
    """The enforcing entry point: ``Supervisor._claim`` for a queued turn."""

    def claim(self, events, comments=()):
        self.set_push(True)
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, fg.HEAD, "fixer", turn_key=f"{CONFLICT_KEY}{'b' * 40}",
                        require_push_admission=True)
        pr = {"number": 7, "state": "open", "draft": False, "head": {"sha": fg.HEAD}}
        loop = {**self.loop, "triage": {"maintainers": [BOSS]}}
        with mock.patch.object(gh, "api", return_value=pr), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(config, "by_repo", return_value=loop), \
             mock.patch.object(gh, "issue_events_read", return_value=(events, "")), \
             mock.patch.object(gh, "issue_comments_read", return_value=(list(comments), "")):
            first = sup._claim()
            return sup, first, sup.get("d")

    def test_a_held_pr_starts_nothing_and_its_turn_stays_queued(self):
        _, claimed, row = self.claim([label(BOSS)])
        self.assertIsNone(claimed)
        self.assertEqual(row["state"], "pending")

    def test_the_queued_turn_resumes_on_release(self):
        sup, claimed, row = self.claim([label(BOSS)])
        self.assertEqual(row["state"], "pending")
        with mock.patch.object(gh, "api", return_value={
                "number": 7, "state": "open", "draft": False, "head": {"sha": fg.HEAD}}), \
             mock.patch.object(config, "by_repo", return_value={
                 **self.loop, "triage": {"maintainers": [BOSS]}}), \
             mock.patch.object(gh, "issue_events_read",
                               return_value=([label(BOSS), label(BOSS, "unlabeled",
                                                                "2026-01-02T00:00:00Z")], "")), \
             mock.patch.object(gh, "issue_comments_read", return_value=([], "")):
            self.assertIsNotNone(sup._claim())
        self.assertEqual(sup.get("d")["state"], "claimed")

    def test_a_non_maintainers_hold_does_not_stop_the_turn(self):
        _, claimed, row = self.claim([label(STRANGER)])
        self.assertIsNotNone(claimed)
        self.assertEqual(row["state"], "claimed")

    def test_an_unreadable_hold_does_not_start_the_turn(self):
        self.set_push(True)
        with mock.patch.object(gh, "issue_events_read", return_value=(None, "event page 1: boom")):
            hold, error = pr_hold.read({**self.loop, "triage": {"maintainers": [BOSS]}}, 7)
        self.assertEqual((hold, error), (None, "event page 1: boom"))

    def test_a_held_pr_starts_no_adjudicator_turn(self):
        """#728: the adjudicator seat waits out a hold like the others."""
        from diaktoros.run_supervisor import pr_facts
        self.set_push(True)
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("adj", fg.REPO, 7, fg.HEAD, "adjudicator", turn_key="breach:1")
        loop = {**self.loop, "triage": {"maintainers": [BOSS]}}
        with mock.patch.object(config, "by_repo", return_value=loop), \
             mock.patch.object(pr_facts, "adjudication_state", return_value=("ok", {})), \
             mock.patch.object(gh, "issue_events_read", return_value=([label(BOSS)], "")), \
             mock.patch.object(gh, "issue_comments_read", return_value=([], "")):
            self.assertIsNone(sup._claim())
        self.assertEqual(sup.get("adj")["state"], "pending")
        with mock.patch.object(config, "by_repo", return_value=loop), \
             mock.patch.object(pr_facts, "adjudication_state", return_value=("ok", {})), \
             mock.patch.object(gh, "issue_events_read", return_value=([], "")), \
             mock.patch.object(gh, "issue_comments_read", return_value=([], "")):
            self.assertIsNotNone(sup._claim())


class Drain(fg.Base):
    def test_the_drain_leaves_a_held_pr_queued(self):
        from scripts import watchdog
        self.set_push(True)
        key = gate.seat_key(self.loop, 7)
        self.st.queue_add("fixer", key, fg.HEAD, "u", "capacity")
        self.st.pr_hold_set(7, {"by": BOSS, "at": "2026-01-01T00:00:00Z", "how": "label"})
        with mock.patch.object(gh, "pr", return_value=fg.LIVE) as read:
            self.assertEqual(watchdog.drain(self.loop, self.st, "fixer", quiet=True), 0)
        read.assert_not_called()
        self.assertIn(key, self.st.queue_items("fixer"))


class Explain(fg.Base):
    def test_explain_names_who_and_when_and_points_at_release(self):
        hold = {"by": BOSS, "at": "2026-01-01T00:00:00Z", "how": "comment"}
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0, "hold": hold}
        report = gate.explain(self.loop, self.st, 7, facts)
        self.assertTrue(any("held by boss" in b and "2026-01-01" in b for b in report["blockers"]))
        self.assertIn("/loop release", report["next"]["action"])

    def test_release_advice_follows_the_hold_type_in_line_and_explain(self):
        comment_hold = {"by": BOSS, "at": "2026-01-01T00:00:00Z", "how": "comment"}
        label_hold = dict(comment_hold, how="label")
        self.assertNotIn("or the label is removed", pr_hold.line(comment_hold))
        self.assertNotIn(pr_hold.LABEL, pr_hold.line(comment_hold).split("until", 1)[1])
        self.assertIn(pr_hold.LABEL, pr_hold.line(label_hold).split("until", 1)[1])
        for hold, names_label in ((comment_hold, False), (label_hold, True)):
            facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0, "hold": hold}
            action = gate.explain(self.loop, self.st, 7, facts)["next"]["action"]
            self.assertIn("/loop release", action)
            self.assertEqual("removal of the diaktoros:hold label" in action, names_label)

    def test_explain_without_a_hold_does_not_mention_one(self):
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0}
        report = gate.explain(self.loop, self.st, 7, facts)
        self.assertFalse(any("held by" in b for b in report["blockers"]))


class Sweep(fg.Base):
    """The watchdog sweep (`_sweep_loop_locked`) leaves a held PR alone (#477)."""
    HOLD = {"by": BOSS, "at": "2026-01-01T00:00:00Z", "how": "label"}

    def sweep(self, read):
        from scripts import watchdog
        dirty = {**fg.LIVE, "mergeable_state": "dirty"}
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(gh, "pr", return_value=dirty) as live, \
             mock.patch.object(gh, "reviews", return_value=[fg.verdict()]), \
             mock.patch.object(pr_hold, "read", side_effect=read), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            lines = watchdog.sweep_loop(self.loop, self.st)
        return "\n".join(lines), live

    def test_a_held_pr_gets_no_stall_report_no_conflict_read_and_is_recorded(self):
        from scripts import watchdog
        gate.hold_fixer_push_off(self.loop, self.st, 7, fg.HEAD)
        self.sweep(lambda *a: (None, ""))                  # baseline
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = 0.0
        self.st.watch_save(watch)
        text, _ = self.sweep(lambda *a: (None, ""))
        self.assertIn(watchdog.PUSH_OFF_KIND, text)        # control: a released PR does stall
        text, live = self.sweep(lambda *a: (self.HOLD, ""))
        self.assertNotIn(watchdog.PUSH_OFF_KIND, text)
        live.assert_not_called()                           # no conflict turn is even considered
        self.assertEqual(self.st.pr_holds(), {7: self.HOLD})
        self.sweep(lambda *a: (self.HOLD, ""))             # still held: the same hold
        with ledger.connect(run_supervisor.production_ledger()) as con:   # the report's (#678)
            self.assertEqual(con.execute("SELECT pr, action FROM operator_actions").fetchall(),
                             [(7, "hold")])

    def test_release_drops_the_record_and_an_unreadable_hold_keeps_it(self):
        self.st.pr_hold_set(7, self.HOLD)
        self.sweep(lambda *a: (None, "event page 1: boom"))
        self.assertEqual(self.st.pr_holds(), {7: self.HOLD})
        self.sweep(lambda *a: (None, ""))
        self.assertEqual(self.st.pr_holds(), {})


if __name__ == "__main__":
    unittest.main()
