"""Issue #663: a maintainer's reply wakes the fixer on an issue it commented on instead of fixing.

Driven through the watchdog's entry point, ``fix_hold.sweep``, against a real run ledger: the
fixer's comment is a recorded, posted issue-fix write, and each queued turn is a ledger row, so
"once per reply" and "not while a turn is queued" are the ledger's own answers.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_issue_fix as base  # noqa: E402
BASE, REPO, issue = base.BASE, base.REPO, base.issue

from diaktoros import fix_hold, fix_reply, gate, gh, observer, prompts  # noqa: E402
from diaktoros import ledger, state as state_mod  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

TURN = time.time() - 3600           # when the fixer's commenting turn was queued


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Replies(base.Gate):
    def setUp(self):
        super().setUp()
        self.st = state_mod.state_for(self.loop)
        self.sup = Supervisor(self.db)
        self.comments = []
        self.reads = 0
        self.notices = []

        def comments(loop, number):
            self.reads += 1
            return self.comments, ""

        def enqueue(loop, seat, n, head, turn_key=""):
            self.enqueued.append((seat, n, head, turn_key))
            self.sup.enqueue(f"{REPO}:{n}:{head}:{seat}:{turn_key}", REPO, n, head, seat,
                             turn_key=turn_key)
            return "enqueued"
        for target, name, value in (
                (gh, "issue_comments_read", comments),
                (gate, "isolated_supervisor", lambda loop: self.sup),
                (gate, "enqueue_isolated", enqueue),
                (observer, "notify", lambda *a, **k: self.notices.append((a, k)))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.comment_turn()

    def comment_turn(self, kind="comment", number=12, at=TURN, state="succeeded", key="issue-fix"):
        """An issue-fix run queued at ``at`` whose one write was ``kind``, now in ``state``."""
        delivery = f"{REPO}:{number}:{BASE}:issue_fixer:{key}"
        self.sup.enqueue(delivery, REPO, number, BASE, "issue_fixer", turn_key=key)
        with ledger.connect(self.db) as con:
            run_id = con.execute("SELECT id FROM runs WHERE delivery=?", (delivery,)).fetchone()[0]
            con.execute("UPDATE runs SET state='running', launch_intent=?, created=? WHERE id=?",
                        (at, at, run_id))
        self.sup.record_issue_fix(run_id, REPO, number, BASE, kind, "")
        self.sup.issue_fix_status(run_id, "posted" if kind == "comment" else "requested",
                                  comment_id=100 if kind == "comment" else None)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
            con.execute("UPDATE issue_fixes SET created=? WHERE run_id=?", (at + 60, run_id))
        return run_id

    def reply(self, cid, who, at=TURN + 600):
        self.comments.append({"id": cid, "user": {"login": who}, "body": "use option B",
                              "created_at": iso(at)})

    def sweep(self):
        return fix_hold.sweep(self.loop, self.st)

    def test_a_maintainers_reply_queues_one_fix_turn_from_the_base(self):
        self.reply(201, "Jeremy")
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        (args, kwargs), = self.notices
        self.assertEqual((args[2], kwargs["identity"], kwargs["outcome_kind"]),
                         ("fixing", "reply201", "queued"))
        self.assertIn("jeremy replied; fix turn queued from main at", kwargs["outcome"])
        self.sweep()                                   # the queued turn is the answer to it
        self.assertEqual(len(self.enqueued), 1)

    def test_two_replies_before_a_sweep_are_one_turn_keyed_by_the_newest(self):
        self.reply(201, "jeremy")
        self.reply(202, "jeremy")
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply202")])

    def test_no_reply_from_a_maintainer_queues_nothing(self):
        self.loop = {**self.loop, "triage": {**self.loop["triage"],
                                             "maintainers": ["jeremy", "fix", "review"]}}
        self.reply(201, "stranger")
        self.reply(202, "fix")                         # the fixer's own login, though listed
        self.reply(203, "review")                      # the triage (reviewer) login, though listed
        self.reply(204, "jeremy", at=TURN - 60)        # before the turn: that turn read it
        self.sweep()
        self.assertEqual(self.enqueued, [])

    def test_an_issue_no_longer_fixable_queues_nothing(self):
        self.reply(201, "jeremy")
        for name, live in (("closed", issue(state="closed")),
                           ("label gone", issue(labels=("bug",)))):
            with self.subTest(name):
                self.live = live
                self.sweep()
                self.assertEqual(self.enqueued, [])

    def test_an_auto_offered_issue_without_the_fix_label_is_not_woken(self):
        # #232: an auto-offer label makes the issue fixable, but only a maintainer's fix label
        # hands it over, so only that one keeps it waiting for a reply.
        self.loop = {**self.loop, "triage": {**self.loop["triage"], "auto_fix_labels": ["bug"]}}
        self.live = issue(labels=("bug",))
        self.reply(201, "jeremy")
        self.sweep()
        self.assertEqual(self.enqueued, [])

    def test_a_later_pr_ends_the_wait(self):
        self.reply(201, "jeremy")
        self.comment_turn("pr", at=TURN + 300, key="issue-fix:again")
        self.sweep()
        self.assertEqual(self.enqueued, [])

    def test_a_later_write_the_broker_refused_does_not_end_the_wait(self):
        run_id = self.comment_turn("pr", at=TURN + 300, key="issue-fix:again")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE issue_fixes SET state='denied' WHERE run_id=?", (run_id,))
        self.reply(201, "jeremy", at=TURN + 900)
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])

    def test_a_turn_already_queued_or_an_open_ask_queues_nothing(self):
        self.reply(201, "jeremy")
        self.sup.enqueue("queued", REPO, 12, "d" * 40, "issue_fixer", turn_key="issue-fix")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending', created=? WHERE delivery='queued'",
                        (TURN - 10,))
        self.sweep()
        self.assertEqual(self.enqueued, [])
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed' WHERE delivery='queued'")
        self.st.fix_ask_set(12, "question", 100, "Which one?")   # sweep_asks owns it
        with mock.patch.object(fix_hold, "sweep_asks"):
            self.sweep()
        self.assertEqual(self.enqueued, [])

    def test_a_reply_during_the_turn_wakes_the_next_one(self):
        self.reply(201, "jeremy", at=TURN + 1200)      # while the second turn ran
        self.comment_turn(at=TURN + 900, key="issue-fix:again")
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])

    def test_an_issue_untouched_since_the_turn_reads_no_comments(self):
        self.live = {**issue(), "updated_at": iso(TURN - 5)}
        self.sweep()
        self.assertEqual(self.reads, 0)
        self.live = {**issue(), "updated_at": iso(TURN + 700)}
        self.reply(201, "jeremy")
        self.sweep()
        self.assertEqual((self.reads, len(self.enqueued)), (1, 1))

    def test_an_unreadable_ledger_skips_the_check_and_the_rest_of_the_sweep_runs(self):
        self.reply(201, "jeremy")
        self.st.fix_hold_set(13, 7)                    # a hand-off whose PR merged meanwhile
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        self.live = issue(number=13)
        with mock.patch.object(gate, "isolated_supervisor", side_effect=RuntimeError("no runtime")):
            self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 13, BASE, "issue-fix")])

    def test_status_and_explain_name_the_wait_until_a_reply_queues_the_turn(self):
        self.sweep()
        self.assertIn(fix_reply.line(12), fix_hold.status_lines(self.loop, self.st))
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [fix_reply.line(12)])
        self.reply(201, "jeremy")
        self.sweep()
        self.assertNotIn(fix_reply.line(12), fix_hold.status_lines(self.loop, self.st))
        self.assertEqual(fix_hold.explain_lines(self.st, 12), [])

    def test_the_prompt_tells_the_fixer_to_act_on_the_reply(self):
        self.assertIn("act on the newest maintainer comment after your own",
                      " ".join(prompts.ISOLATED_ISSUE_FIX.split()))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed fixtures' classes run in test_issue_fix."""
    suite = unittest.TestSuite()
    for name in sorted(vars(Replies)):
        if name.startswith("test_"):
            suite.addTest(Replies(name))
    return suite


if __name__ == "__main__":
    unittest.main()
