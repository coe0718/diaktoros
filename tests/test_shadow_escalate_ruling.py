"""#1004 part 2: a shadow block against the reviewer's approval goes for a ruling.

With ``shadow.escalate`` on, when the reviewer approved a head and the shadow review there
requested changes, the head is sent to the adjudicator once (``gate.shadow_escalation``, from the
shadow's finish and the watchdog sweep). The adjudicator's prompt says the head is approved and
carries the shadow review. ACCEPT overrules it: the hold lifts, ``ready`` and ``explain`` treat the
approval as standing. REJECT keeps the hold; the decision is the operator's (part 3 hands upheld
findings to the fixer).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import gate, run_supervisor, state as state_mod  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

APPROVED = [{"id": 9, "state": "APPROVED", "commit_id": fg.HEAD,
             "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer", "id": 2}}]


class Base(fg.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "shadow": {"profile": "critic", "every": 1, "escalate": True},
                     "adjudicator": {"route": "one-breach"}}
        self.db = run_supervisor.production_ledger()
        run_supervisor.Supervisor(self.db)
        self.n = 0

    def shadow(self, verdict, body="F1: a.py:1: leaks the token", head=fg.HEAD):
        self.n += 1
        now = time.time() + self.n
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                        (f"s{self.n}", f"d{self.n}", fg.REPO, 7, head, "shadow", "succeeded",
                         f"t{self.n}", now, now))
            con.execute("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,"
                        "authorized,denial,held,created) VALUES(?,?,?,?,?,?,?,1,'',0,?)",
                        (f"s{self.n}", fg.REPO, 7, head, "deepseek-x", verdict, body, now))

    def ruled(self, verdict, head=fg.HEAD):
        self.n += 1
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"r{self.n}", fg.REPO, 7, head, "breach:1", verdict, "because",
                         time.time() + self.n, time.time() + self.n))

    def mark_escalated(self, by="shadow", status="adjudicating"):
        marker = {"pr": 7, "head": fg.HEAD, "rounds": 1, "cap": 3, "reason": "r", "at": "now",
                  "status": status, "escalated": {"by": by, "at": "now", "reason": "r"}}
        self.st.breach.parent.mkdir(parents=True, exist_ok=True)
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})
        return marker


class Trigger(Base):
    def escalate(self, reviews=APPROVED, loop=None):
        with mock.patch.object(gate, "breach", return_value="new") as breach:
            outcome = gate.shadow_escalation(loop or self.loop, self.st, 7, fg.HEAD, reviews,
                                             db=self.db)
        return outcome, breach

    def test_a_shadow_block_on_an_approved_head_goes_for_a_ruling_once(self):
        self.shadow("REQUEST_CHANGES")
        outcome, breach = self.escalate()
        self.assertEqual(outcome, "new")
        args, kwargs = breach.call_args
        # PR, head, and rounds of at least 1: the breach turn key is breach:<rounds>, and the
        # adjudicator's eligibility refuses rounds < 1 (shadow review of #1103).
        self.assertEqual((args[2], args[3], args[4]), (7, fg.HEAD, 1))
        self.assertEqual(kwargs["escalated"]["by"], gate.SHADOW_ESCALATION)
        self.mark_escalated()
        self.assertEqual(self.escalate()[0], "already")

    def test_nothing_goes_when_any_condition_is_missing(self):
        self.shadow("REQUEST_CHANGES")
        changes = [{**APPROVED[0], "state": "CHANGES_REQUESTED"}]
        off = {**self.loop, "shadow": {"profile": "critic", "every": 1}}
        for name, call, want in (
                ("escalate off", dict(loop=off), "off"),
                ("no adjudicator", dict(loop={**self.loop, "adjudicator": {}}), "no adjudicator"),
                ("reviewer blocked too", dict(reviews=changes), "not approved"),
                ("reviews unreadable", dict(reviews=None), "reviews unreadable")):
            with self.subTest(name):
                outcome, breach = self.escalate(**call)
                self.assertEqual(outcome, want)
                breach.assert_not_called()
        self.shadow("APPROVE")                                # the newest shadow review agrees
        self.assertEqual(self.escalate()[0], "shadow agrees")


class Hold(Base):
    def hold(self, marker):
        return gate.shadow_hold(self.loop, 7, fg.HEAD, db=self.db, marker=marker)

    def test_the_hold_follows_the_ruling(self):
        self.shadow("REQUEST_CHANGES")
        marker = self.mark_escalated()
        self.assertIn("waiting for its ruling", self.hold(marker)[0])
        self.ruled("REJECT")
        self.assertIn("ruled REJECT on the shadow review", self.hold(marker)[0])
        self.ruled("ACCEPT")                                  # the newest ruling wins
        held, note = self.hold(marker)
        self.assertEqual(held, "")
        self.assertIn("overruled the shadow review", note)

    def test_an_accept_never_overrules_a_newer_shadow_review(self):
        # Review on #1103 (F2): a ruling answers the review it was shown; a newer dissent at the
        # same head is not overruled by it.
        self.shadow("REQUEST_CHANGES")
        marker = self.mark_escalated()
        self.ruled("ACCEPT")
        self.assertEqual(self.hold(marker)[0], "")
        self.shadow("REQUEST_CHANGES")                      # a re-review dissents again
        self.assertIn("waiting for its ruling", self.hold(marker)[0])
        self.assertFalse(gate.shadow_overruled(self.loop, marker, fg.HEAD, db=self.db))

    def test_another_escalation_or_head_is_not_this_shadows(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("ACCEPT")
        dispute = self.mark_escalated(by="dispute")
        self.assertIn("holds the PR until you decide", self.hold(dispute)[0])
        self.assertTrue(gate.shadow_overruled(self.loop, self.mark_escalated(), fg.HEAD,
                                              db=self.db))
        self.assertFalse(gate.shadow_overruled(self.loop, dispute, fg.HEAD, db=self.db))
        self.assertFalse(gate.shadow_overruled(self.loop, self.mark_escalated(), "b" * 40,
                                               db=self.db))


class Eligibility(Base):
    """The adjudicator's own claim check (``pr_facts.adjudication_state``), not a mock: the
    shadow review of #1103 found every shadow escalation was superseded there before it could
    rule (rounds 0, approved at the head, a review-only author)."""

    def state(self, reviews, author="fixer", marker_by="shadow", rounds=1):
        from diaktoros import gh
        from diaktoros.run_supervisor import pr_facts
        marker = self.mark_escalated(by=marker_by, status="awaiting-adjudication")
        marker["rounds"] = rounds
        self.st.breach.parent.mkdir(parents=True, exist_ok=True)
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}, "user": {"login": author}}
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD, "seat": "adjudicator",
               "turn_key": f"breach:{rounds}", "id": "r1", "created": time.time()}
        loop = {**self.loop, "review_only": ["boss"]}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=list(reviews)), \
                mock.patch.object(pr_facts, "effective_reviews",
                                  side_effect=lambda loop, row, reviews, db=None: reviews):
            return pr_facts.adjudication_state(loop, row)[0]

    def test_a_shadow_escalation_on_an_approved_head_is_eligible(self):
        self.assertEqual(self.state(APPROVED), "ok")
        self.assertEqual(self.state(APPROVED, author="boss"), "ok")     # review-only author

    def test_other_escalations_keep_their_rules(self):
        changes = [{**APPROVED[0], "state": "CHANGES_REQUESTED"}]
        self.assertEqual(self.state(APPROVED, marker_by="dispute"), "superseded")
        self.assertEqual(self.state(changes, marker_by="dispute"), "ok")
        self.assertEqual(self.state(changes), "superseded")      # the approval is gone: moot
        self.assertEqual(self.state(APPROVED, author="boss", marker_by="dispute"), "superseded")
        self.assertEqual(self.state(APPROVED, rounds=0), "superseded")

    def test_the_trigger_and_the_claim_agree_end_to_end(self):
        # The trigger's real breach marker, then the real claim check on it.
        from diaktoros import gh
        self.shadow("REQUEST_CHANGES")
        with mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True), \
                mock.patch.object(gate.observer, "notify"):
            self.assertEqual(gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD, APPROVED,
                                                    db=self.db), "new")
        marker = self.st.breach_get(7)
        from diaktoros.run_supervisor import pr_facts
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}}
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD, "seat": "adjudicator",
               "turn_key": f"breach:{marker['rounds']}", "id": "r1", "created": time.time()}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=list(APPROVED)), \
                mock.patch.object(pr_facts, "effective_reviews",
                                  side_effect=lambda loop, row, reviews, db=None: reviews):
            self.assertEqual(pr_facts.adjudication_state(self.loop, row)[0], "ok")


    def test_a_review_only_authors_head_goes_for_a_ruling(self):
        # breach()'s own live-head check, not just the claim's: a review-only PR's shadow
        # escalation is written and woken, while other escalations stay the fixer's.
        from diaktoros import gh
        self.shadow("REQUEST_CHANGES")
        loop = {**self.loop, "review_only": ["boss"]}
        live = {**fg.LIVE, "user": {"login": "Boss"}}
        with mock.patch.object(gh, "pr", return_value=live), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake, \
                mock.patch.object(gate.observer, "notify"):
            self.assertEqual(gate.breach(loop, self.st, 7, fg.HEAD, 1, "r",
                                         escalated={"by": "dispute"}), "stale")
            self.assertEqual(gate.shadow_escalation(loop, self.st, 7, fg.HEAD, APPROVED,
                                                    db=self.db), "new")
        wake.assert_called_once()
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")


class Again(Base):
    """#1111 (review on #1103): after the ruling, a newer shadow dissent at the same head goes
    for its own ruling, as the next round, instead of waiting on one nothing would request."""

    def escalate(self):
        from diaktoros import gh
        with mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate.observer, "notify"), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake:
            outcome = gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD, APPROVED, db=self.db)
        return outcome, [c.args[3] for c in wake.call_args_list]

    def claim(self, rounds):
        from diaktoros import gh
        from diaktoros.run_supervisor import pr_facts
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD, "seat": "adjudicator",
               "turn_key": f"breach:{rounds}", "id": "r1", "created": time.time()}
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=list(APPROVED)), \
                mock.patch.object(pr_facts, "effective_reviews",
                                  side_effect=lambda loop, row, reviews, db=None: reviews):
            return pr_facts.adjudication_state(self.loop, row)[0]

    def test_a_dissent_after_the_ruling_goes_for_the_next_one(self):
        self.shadow("REQUEST_CHANGES")
        self.assertEqual(self.escalate(), ("new", [1]))
        self.assertIsNotNone(self.st.breach_start(7, fg.HEAD, 1))   # the adjudicator's turn
        self.ruled("ACCEPT")                                        # turn key breach:1
        self.assertEqual(self.escalate(), ("already", []))          # no newer dissent
        self.assertEqual(gate.shadow_hold(self.loop, 7, fg.HEAD, db=self.db,
                                          marker=self.st.breach_get(7))[0], "")
        self.shadow("REQUEST_CHANGES")                              # a re-review dissents
        self.assertEqual(self.escalate(), ("new", [2]))
        marker = self.st.breach_get(7)
        self.assertEqual((marker["rounds"], marker["status"]), (2, "awaiting-adjudication"))
        self.assertIn("again", marker["reason"])
        self.assertEqual(self.claim(2), "ok")                       # its own turn is claimable
        self.assertEqual(self.claim(1), "superseded")
        self.assertEqual(self.escalate(), ("already", []))          # once per dissent
        self.assertIn("waiting for its ruling",
                      gate.shadow_hold(self.loop, 7, fg.HEAD, db=self.db, marker=marker)[0])

    def test_a_ruling_still_out_is_not_doubled(self):
        self.shadow("REQUEST_CHANGES")
        self.escalate()
        self.st.breach_start(7, fg.HEAD, 1)
        self.shadow("REQUEST_CHANGES")              # lands while the first ruling is out
        self.assertEqual(self.escalate(), ("already", []))
        self.assertEqual(self.st.breach_get(7)["rounds"], 1)

class Retry(Base):
    """A failed adjudicator wake is retried by the next check (#1103's shadow review): the
    sweep's delivery retry skips approved heads, and every shadow escalation is at one."""

    def test_a_failed_wake_is_sent_again(self):
        from diaktoros import gh
        self.shadow("REQUEST_CHANGES")
        with mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate.observer, "notify"):
            with mock.patch.object(gate, "wake_adjudicator", return_value=False):
                self.assertEqual(gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD,
                                                        APPROVED, db=self.db), "new")
            self.assertEqual(self.st.breach_get(7)["status"], "delivery-pending")
            with mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake:
                self.assertEqual(gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD,
                                                        APPROVED, db=self.db), "retry")
                self.assertEqual(gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD,
                                                        APPROVED, db=self.db), "already")
        wake.assert_called_once()
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_another_escalations_pending_wake_is_left_to_its_own_retry(self):
        self.shadow("REQUEST_CHANGES")
        self.mark_escalated(by="dispute", status="delivery-pending")
        with mock.patch.object(gate, "breach") as breach:
            self.assertEqual(gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD, APPROVED,
                                                    db=self.db), "already")
        breach.assert_not_called()

class Explain(Base):
    def report(self):
        facts = {"pr": fg.LIVE, "reviews": APPROVED, "armed": True, "read_at": 0,
                 "shadow_hold": ("", "")}
        return gate.explain(self.loop, self.st, 7, facts)

    def test_an_overruled_shadow_escalation_reads_as_approved(self):
        self.mark_escalated()
        with mock.patch.object(gate, "_ruled", return_value="ACCEPT"):
            report = self.report()
        self.assertFalse(any(b.startswith(("ruled:", "escalated:")) for b in report["blockers"]),
                         report["blockers"])
        self.assertIn("a human merges it", report["next"]["action"])

    def test_a_disputes_accept_still_leaves_the_decision_to_the_operator(self):
        self.mark_escalated(by="dispute")
        with mock.patch.object(gate, "_ruled", return_value="ACCEPT"):
            report = self.report()
        self.assertTrue(any(b.startswith("ruled:") for b in report["blockers"]), report["blockers"])


class Ready(Base):
    def test_ready_does_not_park_an_overruled_shadow_escalation(self):
        from diaktoros import ready
        self.mark_escalated()
        pr = {**fg.LIVE, "user": {"login": "fixer"}}
        with mock.patch.object(gate, "shadow_hold", return_value=("", "")), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            with mock.patch.object(gate, "shadow_overruled", return_value=False):
                parked = ready.assess(self.loop, self.st, pr, APPROVED, None, None)
            with mock.patch.object(gate, "shadow_overruled", return_value=True):
                free = ready.assess(self.loop, self.st, pr, APPROVED, None, None)
        self.assertIn("escalated: parked awaiting adjudication", parked)
        self.assertNotIn("escalated: parked awaiting adjudication", free)


class Prompt(Base):
    def test_the_ruling_prompt_says_the_head_is_approved_and_carries_the_shadow_review(self):
        self.shadow("REQUEST_CHANGES", body="F1: gate.py:187: uncertain holds forever")
        situation = turn_prompts.adjudication_situation({"escalated": {"by": "shadow"}})
        self.assertIn("**approved** this head", situation)
        self.assertNotIn("none an approval", situation)
        self.assertIn("budget is spent", turn_prompts.adjudication_situation({}))
        self.assertIn("pushed nothing", turn_prompts.adjudication_situation(
            {"escalated": {"by": "dispute"}}))
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD}
        text = turn_prompts.shadow_review_section(self.loop, row)
        self.assertIn("## Second review", text)
        self.assertIn("deepseek-x", text)
        self.assertIn("F1: gate.py:187: uncertain holds forever", text)


class Wiring(Base):
    def test_the_adjudicators_prompt_includes_the_shadow_review(self):
        from diaktoros import ci, gh, issue_facts
        from diaktoros.run_supervisor import pr_facts
        self.shadow("REQUEST_CHANGES", body="F1: gate.py:187: uncertain holds forever")
        row = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD, "seat": "adjudicator"}
        marker = self.mark_escalated()
        with mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(ci, "read", return_value=None), \
                mock.patch.object(ci, "section", return_value=""), \
                mock.patch.object(pr_facts, "pr_record", return_value="RECORD"):
            text = turn_prompts.isolated_prompt(self.loop, row, list(APPROVED), marker=marker)
            plain = turn_prompts.isolated_prompt(self.loop, row, list(APPROVED),
                                                 marker={**marker, "escalated": {"by": "dispute"}})
        self.assertIn("**approved** this head", text)
        self.assertIn("F1: gate.py:187: uncertain holds forever", text)
        self.assertNotIn("## Second review", plain)

    def test_the_escalation_notice_names_the_shadow(self):
        from diaktoros import gh
        with mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True), \
                mock.patch.object(gate.observer, "notify") as notify:
            outcome = gate.breach(self.loop, self.st, 7, fg.HEAD, 0, "r",
                                  escalated={"by": gate.SHADOW_ESCALATION, "at": "now",
                                             "reason": "r"})
        self.assertEqual(outcome, "new")
        self.assertIn("shadow review requested changes", notify.call_args.kwargs["outcome"])


class CallSites(Base):
    """Both places that run the check: the shadow's finish and the watchdog sweep."""

    def sweep(self):
        from diaktoros import gh, pr_hold
        from scripts import watchdog
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(gh, "pr", return_value=fg.LIVE), \
             mock.patch.object(gh, "reviews", return_value=list(APPROVED)), \
             mock.patch.object(pr_hold, "read", return_value=(None, "")), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            watchdog.sweep_loop(self.loop, self.st)

    def test_the_sweep_checks_an_approved_head(self):
        calls = []
        with mock.patch.object(gate, "shadow_escalation",
                               side_effect=lambda *a, **k: calls.append(a[2:4]) or "new"):
            self.sweep()                                      # first sweep: snapshot only
            self.sweep()
        self.assertIn((7, fg.HEAD), calls)

    def test_the_shadows_finish_checks_once_its_review_is_recorded(self):
        import test_shadow as ts
        turn = ts.ProductionTurn("run")
        turn.setUp()
        self.addCleanup(turn.doCleanups)
        with mock.patch.object(run_supervisor.Supervisor, "_shadow_escalation") as hook:
            turn.run_shadow([{"verdict": "REQUEST_CHANGES", "body": "F1: a.py:1: x",
                              "authorized": True, "denial": ""}])
        hook.assert_called_once()
        with mock.patch.object(run_supervisor.Supervisor, "_shadow_escalation") as hook:
            turn.setUp()
            turn.run_shadow([], rc=1)                         # nothing recorded: no check
        hook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
