"""Issue-triggered fixes (#214): a maintainer's fix label hands an issue to the fixer seat.

No real GitHub, model, Hermes or ~/.hermes: GitHub is mocked and the ledger lives in a private
temporary directory. The one Git test runs real ``git`` against local bare repositories through
``safe_push``'s private remote seam. What is pinned: only a maintainer's label on an allowlisted
author's issue starts a turn, only with unattended fixer pushes on; the turn's one write is a
fresh branch (never an existing one) with its PR and review request, or one comment; everything
is recorded before it is written.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diaktoros import ledger  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros import (broker, broker_ipc, config, fix_hold, gate, gh, observer, run_supervisor,  # noqa: E402
                         seat_model, safe_push, trusted_turn)
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
BASE = "c" * 40
LABEL = "agent-fix"


def issue(number=12, author="owner", state="open", labels=(LABEL,), body="Typo in README."):
    return {"number": number, "state": state, "user": {"login": author}, "title": "Fix the typo",
            "body": body, "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.com/{REPO}/issues/{number}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / f"{login}.pat"
            path.write_text("DUMMY_SECRET_" + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        self.raw = {"id": "widgets", "repo": REPO, "fixers": ["fix"], "reviewers": ["review"],
                    "tokens": tokens, "read_token": "read", "host": "https://gw.example",
                    "state_dir": str(self.root / "state"), "unattended_fixer_push": True,
                    "seats": {"reviewer": {"route": "widgets-review", "profile": "critic",
                                           "login": "review"},
                              "fixer": {"route": "widgets-fix", "profile": "coder",
                                        "login": "fix"}},
                    "triage": {"route": "widgets-triage", "profile": "arbiter",
                               "authors": ["owner"], "labels": ["bug", "docs"],
                               "fix_label": LABEL, "maintainers": ["Jeremy"]}}
        self.loop = config.normalize(self.raw)
        self.db = self.root / "runs.sqlite"

    def fix_row(self, state="running", number=12):
        sup = Supervisor(self.db)
        sup.enqueue(f"{REPO}:{number}:{BASE}:issue_fixer:issue-fix", REPO, number, BASE,
                    "issue_fixer", turn_key="issue-fix")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=? WHERE seat='issue_fixer'", (state,))
            if state in ("launching", "running"):
                con.execute("UPDATE runs SET launch_intent=? WHERE seat='issue_fixer'",
                            (time.time(),))
            return sup, con.execute("SELECT id FROM runs WHERE seat='issue_fixer'").fetchone()[0]


class Config(Base):
    def test_issue_fixes_need_a_label_maintainers_and_unattended_pushes(self):
        self.assertTrue(config.issue_fixes_enabled(self.loop))
        self.assertEqual(self.loop["triage"]["maintainers"], ["jeremy"])
        off = config.normalize({**self.raw, "unattended_fixer_push": False})
        self.assertFalse(config.issue_fixes_enabled(off))
        for name, change in (("no maintainers", {"maintainers": []}),
                             ("label triage could apply", {"fix_label": "docs"})):
            with self.subTest(name), self.assertRaises(config.ConfigError):
                config.normalize({**self.raw, "triage": {**self.raw["triage"], **change}})
        # #1217: maintainers stand on their own; without a label they keep the loop's trust
        # (comments to the seats, re-requests, holds), and issue fixes are simply off.
        paused = config.normalize({**self.raw, "triage": {**self.raw["triage"], "fix_label": "",
                                                          "maintainers": ["X"]}})
        self.assertEqual(config.maintainers(paused), {"x"})
        self.assertFalse(config.issue_fixes_enabled(paused))

    def test_the_issue_fixer_is_the_fixer_seat(self):
        self.assertEqual(config.seat_profile(self.loop, "issue_fixer"), "coder")
        self.assertEqual(config.seat_login(self.loop, "issue_fixer"), "fix")
        self.assertEqual(config.seat_concurrency(self.loop, "issue_fixer"), 1)


class Gate(Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("gate_triage_fix_under_test",
                                                      ROOT / "scripts" / "gate_triage.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.live = issue()
        self.enqueued = []
        self.open_prs = []
        self.posted = []
        self.origin = None          # the PR a seat filed the issue from (#324)
        self.origin_pr = {"number": 7, "state": "open", "merged": False}

        def api(loop, path, method="GET", body=None, login=None):
            if "/pulls/" in path:
                return self.origin_pr
            if path.endswith(f"/git/ref/heads/{self.loop['base']}"):
                return {"ref": "refs/heads/main", "object": {"sha": BASE}}
            if method == "POST":
                self.posted.append((path, body, login))
                return {"id": 1}
            return self.live
        for target, name, value in (
                (fix_hold, "origin_pr", lambda loop, number: self.origin),
                (gh, "open_prs_read", lambda loop: (self.open_prs, "")),
                (gate, "context", lambda payload: (self.loop, None)),
                (gh, "api", api),
                (gate, "enqueue_isolated",
                 lambda loop, seat, n, head, turn_key="": self.enqueued.append(
                     (seat, n, head, turn_key)) or "enqueued")):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_gate(self, payload) -> str:
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            self.module.main()
        return err.getvalue()

    def labeled(self, label=LABEL, sender="jeremy"):
        return {"action": "labeled", "label": {"name": label}, "issue": issue(),
                "sender": {"login": sender}, "repository": {"full_name": REPO}}

    def test_a_maintainers_fix_label_queues_one_fix_from_the_base_head(self):
        self.run_gate(self.labeled(label="Agent-Fix"))
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])

    def test_an_operator_agents_fix_label_hands_off_and_a_strangers_does_not(self):
        # #757 (the shadow on #1329): an operator agent speaks with maintainer authority, so its
        # fix label starts the fix; anyone else's is still silent.
        self.loop = {**self.loop, "operator_agents": ["quill-bot"]}
        self.run_gate(self.labeled(label="Agent-Fix", sender="stranger"))
        self.assertEqual(self.enqueued, [])
        self.run_gate(self.labeled(label="Agent-Fix", sender="Quill-Bot"))
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])

    def test_the_fix_handoff_is_recorded_as_accepted(self):
        # #1244 (live trial): the agent-fix label that started the fix was missing from the log.
        from diaktoros import util
        recorded = []
        with mock.patch.object(util, "_DECISION_RECORDER",
                               lambda reason, decision: recorded.append((decision, reason))):
            self.run_gate(self.labeled(label="Agent-Fix"))
        self.assertEqual(recorded, [("accepted",
                                     f"issue #12 handed to the fixer at {BASE[:7]}: enqueued")])

    def test_an_issue_the_loop_filed_itself_can_be_fixed_and_a_strangers_cannot(self):
        # #1794: shadow-found issues are filed as the reader (an App's bot since the reader moved
        # to one) and reviewer-filed issues as the reviewer; neither is in triage.authors here.
        self.assertNotIn("read", self.loop["triage"]["authors"])
        for author, fixed in (("read", True), ("Review", True), ("stranger", False)):
            with self.subTest(author):
                self.enqueued.clear()
                self.live = issue(author=author)
                self.run_gate(self.labeled())
                self.assertEqual(bool(self.enqueued), fixed)

    def test_a_declined_fix_label_is_said_once_per_reason_and_listed_until_accepted(self):
        # #1794: a maintainer's label on an issue that can't be fixed was declined silently.
        from diaktoros import observer
        said = []
        self.live = issue(author="stranger")
        with mock.patch.object(observer, "notify",
                               side_effect=lambda *a, **kw: said.append((a[2], a[3], kw))):
            for _ in range(2):
                self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        self.assertEqual([(event, number) for event, number, _ in said], [("held", 12)] * 2)
        identities = {kw["identity"] for _, _, kw in said}
        self.assertEqual(len(identities), 1)       # one identity: the observer says it once
        self.assertIn("triage --loop widgets --enable --author", said[0][2]["outcome"])
        st = state_mod.state_for(self.loop)
        (line,) = [x for x in fix_hold.status_lines(self.loop, st) if "#12" in x]
        self.assertIn("the fix label was declined", line)
        self.live = issue()                         # its author allowed now: accepted
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual([x for x in fix_hold.status_lines(self.loop, st) if "#12" in x], [])

    def test_a_declined_next_step_says_to_remove_the_label_before_applying_it(self):
        # #1833: the label is still on the issue when the decline is recorded, and GitHub sends a
        # `labeled` event only when one is added: "apply it again" produced no event.
        from diaktoros import observer
        said = []
        st = state_mod.state_for(self.loop)
        cases = [("author", {}), ("pushes off", {"unattended_fixer_push": False})]
        for name, change in cases:
            with self.subTest(name):
                self.loop = {**self.loop, **change}
                self.live = issue(author="stranger")
                said.clear()
                with mock.patch.object(observer, "notify",
                                       side_effect=lambda *a, **kw: said.append(kw["outcome"])):
                    self.run_gate(self.labeled())
                (line,) = [x for x in fix_hold.status_lines(self.loop, st) if "#12" in x]
                for text in (said[0], line):
                    self.assertIn("remove the fix label and apply it again", text)
                    self.assertNotIn("apply the label again", text)
        from diaktoros import fix_declined
        for reason in ("no longer eligible: the issue is no longer open", "something else"):
            self.assertIn("remove the fix label and apply it again",
                          fix_declined.next_step(self.loop, reason))

    def test_a_decline_for_a_label_already_gone_says_to_apply_it_not_remove_it(self):
        # Review of #1834, F1: a delayed `labeled` delivery finds the label already off the live
        # issue. Removing an absent label is impossible, and adding it is itself the new event.
        from diaktoros import observer
        said = []
        self.live = issue(labels=())
        with mock.patch.object(observer, "notify",
                               side_effect=lambda *a, **kw: said.append(kw["outcome"])):
            self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        st = state_mod.state_for(self.loop)
        (line,) = [x for x in fix_hold.status_lines(self.loop, st) if "#12" in x]
        self.assertIn("is no longer on the issue", line)
        for text in (said[0], line):
            self.assertTrue(text.endswith("apply the fix label to the issue"), text)
            self.assertNotIn("remove the fix label", text)

    def test_an_absent_label_with_another_cause_still_says_to_apply_it(self):
        # Review of #1834, F2: the label is off AND pushes are off / the author is not allowed.
        # The gate checks those first, so the recorded reason never mentions the label; the live
        # issue decides the step.
        from diaktoros import observer
        st = state_mod.state_for(self.loop)
        cases = [("pushes off", {"unattended_fixer_push": False}, "owner"),
                 ("author", {}, "stranger")]
        for name, change, author in cases:
            with self.subTest(name):
                self.loop = {**self.loop, **change}
                self.live = issue(author=author, labels=())
                said = []
                with mock.patch.object(observer, "notify",
                                       side_effect=lambda *a, **kw: said.append(kw["outcome"])):
                    self.run_gate(self.labeled())
                self.assertEqual(self.enqueued, [])
                (line,) = [x for x in fix_hold.status_lines(self.loop, st) if "#12" in x]
                for text in (said[0], line):
                    self.assertTrue(text.endswith("apply the fix label to the issue"), text)
                    self.assertNotIn("remove the fix label", text)

    def test_the_neither_label_decline_says_to_apply_it(self):
        # Review of #1834, F3: an auto-offer loop whose issue has no fix and no offer label.
        from diaktoros import fix_declined, observer
        raw = {**self.raw, "triage": {**self.raw["triage"], "labels": ["bug", "P3", "docs"],
                                      "auto_fix_labels": ["P3", "docs"]}}
        self.loop = config.normalize(raw)
        self.live = issue(labels=("bug",))
        said = []
        with mock.patch.object(observer, "notify",
                               side_effect=lambda *a, **kw: said.append(kw["outcome"])):
            self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        (line,) = [x for x in fix_hold.status_lines(self.loop, st) if "#12" in x]
        self.assertIn("neither the fix label", line)
        for text in (said[0], line):
            self.assertTrue(text.endswith("apply the fix label to the issue"), text)
        # And from the reason alone, when the live issue can't be read for the notice.
        reason = "no longer eligible: neither the fix label ('agent-fix') nor an auto-offer label"
        self.assertEqual(fix_declined.next_step(self.loop, reason),
                         "once that is fixed, apply the fix label to the issue")

    def test_an_issue_filed_by_an_app_reader_is_handed_to_the_fixer(self):
        # #1794 (review of #1804, F4): the reader as a GitHub App, filing as its bot.
        bot = "widgets-reader[bot]"
        self.loop = {**self.loop, "read_token": bot,
                     "apps": {bot: {"app_id": 9, "key_file": str(self.root / "reader.pem")}}}
        self.assertNotIn(bot, self.loop["triage"]["authors"])
        self.live = issue(author="Widgets-Reader[bot]")
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])

    def test_declines_reach_the_observer_once_per_reason(self):
        # #1804, F3 and F4: through the observer's own ledger. A repeat of one reason is not sent
        # again; a second reason (pushes turned off) is, with its own next step.
        from diaktoros import observer, routes
        subs = self.root / "subscriptions.json"
        self.loop = {**self.loop, "host": "https://owner.example",
                     "observer": {"route": "widgets-observe", "profile": "default",
                                  "deliver": "telegram"}}
        subs.write_text(json.dumps({"widgets-observe": {
            "host": "https://owner.example", "secret": "s", **observer.route_contract(self.loop)}}))
        self.live = issue(author="stranger")
        with mock.patch.dict(os.environ, {"DIAKTOROS_SUBS": str(subs)}), \
                mock.patch.object(routes, "fire", return_value=True) as fire:
            self.run_gate(self.labeled())
            self.run_gate(self.labeled())
            self.loop = {**self.loop, "unattended_fixer_push": False}
            self.run_gate(self.labeled())
        held = [c.args[2]["_observer"] for c in fire.call_args_list
                if c.args[2]["_observer"]["event"] == "held"]
        self.assertEqual(len(held), 2, held)
        text = json.dumps(held)
        self.assertIn("triage --loop widgets --enable --author", text)
        self.assertIn("fixer-push", text)

    def test_every_outstanding_decline_is_listed_until_the_issue_closes_or_the_label_goes(self):
        # #1804, F2: keeping the newest 20 dropped older declines nobody had resolved.
        st = state_mod.state_for(self.loop)
        for number in range(1, 26):
            self.live = issue(number=number, author="stranger")
            payload = self.labeled()
            payload["issue"] = issue(number=number)
            self.run_gate(payload)
        listed = [x for x in fix_hold.status_lines(self.loop, st) if "fix label was declined" in x]
        self.assertEqual(len(listed), 25)
        # The live issues as the events left them: the gate reads them before it settles (F8).
        live = {3: issue(number=3, author="stranger", state="closed"),
                4: issue(number=4, author="stranger", labels=()),
                5: issue(number=5, author="stranger")}
        with mock.patch.object(gh, "api", lambda loop, path, method="GET", body=None, login=None:
                               live.get(int(path.rsplit("/", 1)[1]))):
            for action, label in (("closed", None), ("unlabeled", LABEL), ("unlabeled", "bug")):
                number = {"closed": 3, LABEL: 4, "bug": 5}[label or action]
                self.run_gate({"action": action, "label": {"name": label} if label else None,
                               "issue": issue(number=number), "sender": {"login": "jeremy"},
                               "repository": {"full_name": REPO}})
        still = {int(x.split("#")[1].split(":")[0]) for x in fix_hold.status_lines(self.loop, st)
                 if "fix label was declined" in x}
        self.assertNotIn(3, still)                  # closed
        self.assertNotIn(4, still)                  # the fix label removed
        self.assertIn(5, still)                     # another label removed: still declined
        self.assertEqual(len(still), 23)

    def test_an_issue_the_loop_filed_needs_the_live_fix_label_not_an_auto_offer(self):
        # Review of #1804, F5: an auto-offer label stood in for the fix label on the loop's own
        # issues too, so a stale label event started a fix after the label came off. A person
        # applies the fix label to those; an auto-offer still counts for a listed author.
        self.loop = {**self.loop, "triage": {**self.loop["triage"], "auto_fix_labels": ["bug"]}}
        for author, labels, fixed in (("read", ("bug",), False),     # label removed before the gate
                                      ("read", (LABEL, "bug"), True),
                                      ("owner", ("bug",), True)):
            with self.subTest(author=author, labels=labels):
                self.enqueued.clear()
                self.live = issue(author=author, labels=labels)
                self.run_gate(self.labeled())
                self.assertEqual(bool(self.enqueued), fixed)
        from diaktoros.run_supervisor import issue_inputs
        self.live = issue(author="read", labels=("bug",))
        with self.assertRaisesRegex(run_supervisor.IssueIneligible, "auto-offer label does not"):
            issue_inputs.issue_fix_issue(self.loop, 12)       # the launch and broker recheck too

    def test_a_decline_is_settled_when_a_later_label_waits_on_a_pr(self):
        # Review of #1804, F6: accepted into a wait, then released by the sweep, the old decline
        # stayed listed, telling the operator to allow an author already allowed.
        st = state_mod.state_for(self.loop)
        self.live = issue(author="stranger")
        self.run_gate(self.labeled())
        self.assertTrue([x for x in fix_hold.status_lines(self.loop, st) if "declined" in x])
        self.loop = {**self.loop, "triage": {**self.loop["triage"],
                                             "authors": ["owner", "stranger"]}}
        self.origin = 7                              # waits on open PR #7
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        self.assertEqual(st.fix_holds(), {12: 7})
        self.assertEqual([x for x in fix_hold.status_lines(self.loop, st) if "declined" in x], [])
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, st)
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual([x for x in fix_hold.status_lines(self.loop, st) if "declined" in x], [])

    def test_a_decline_stays_listed_when_the_later_hand_off_fails(self):
        # Review of #1804, F7: cleared before the hand-off ran, a failed hand-off (here the open
        # PR listing unreadable) left neither the decline nor a wait nor a queued fix.
        st = state_mod.state_for(self.loop)
        self.live = issue(author="stranger")
        self.run_gate(self.labeled())
        self.loop = {**self.loop, "triage": {**self.loop["triage"],
                                             "authors": ["owner", "stranger"]}}
        with mock.patch.object(gh, "open_prs_read", lambda loop: (None, "offline")):
            log = self.run_gate(self.labeled())
        self.assertIn("open PR listing unreadable", log)
        self.assertEqual(self.enqueued, [])
        self.assertTrue([x for x in fix_hold.status_lines(self.loop, st) if "declined" in x])
        self.run_gate(self.labeled())                # the redelivery hands it off: settled
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual([x for x in fix_hold.status_lines(self.loop, st) if "declined" in x], [])

    def test_a_late_close_or_unlabel_event_does_not_erase_a_newer_decline(self):
        # Review of #1804, F8: the label removed and applied again, the new label declined, and
        # then the old unlabeled event (or a redelivered closed one) arrives. The issue is open
        # and labelled, so the newer request is still outstanding.
        st = state_mod.state_for(self.loop)
        self.live = issue(author="stranger")
        self.run_gate(self.labeled())
        for action, label in (("unlabeled", LABEL), ("closed", None)):
            with self.subTest(action):
                self.run_gate({"action": action, "label": {"name": label} if label else None,
                               "issue": issue(), "sender": {"login": "jeremy"},
                               "repository": {"full_name": REPO}})
                self.assertTrue([x for x in fix_hold.status_lines(self.loop, st)
                                 if "declined" in x])

    def test_a_decline_recorded_while_the_live_read_ran_is_kept(self):
        # Review of #1804, F8: the read showed the label gone, and a newer label was declined
        # before the clear; only the decline seen before the read may go.
        from diaktoros import fix_declined
        st = state_mod.state_for(self.loop)
        self.live = issue(author="stranger")
        self.run_gate(self.labeled())

        def read_then_relabel(loop, path, method="GET", body=None, login=None):
            fix_declined.declined(self.loop, 12, "a newer reason")    # lands mid-read
            return issue(author="stranger", labels=())
        with mock.patch.object(gh, "api", read_then_relabel):
            fix_declined.settle(self.loop, st, 12)
        self.assertEqual(fix_declined.entries(st)[12]["reason"], "a newer reason")

    def test_the_sweep_settles_a_decline_whose_close_or_unlabel_event_was_missed(self):
        # Review of #1804, F6: with the event lost, the decline was listed forever.
        st = state_mod.state_for(self.loop)
        for number in (1, 2, 3, 4):
            self.live = issue(number=number, author="stranger")
            payload = self.labeled()
            payload["issue"] = issue(number=number)
            self.run_gate(payload)
        live = {1: issue(number=1, author="stranger", state="closed"),
                2: issue(number=2, author="stranger", labels=("bug",)),
                3: issue(number=3, author="stranger")}          # 4 is unreadable

        def api(loop, path, method="GET", body=None, login=None):
            self.assertEqual(login, "read")
            return live.get(int(path.rsplit("/", 1)[1]))
        with mock.patch.object(gh, "api", api):
            fix_hold.sweep(self.loop, st)
        still = {int(x.split("#")[1].split(":")[0]) for x in fix_hold.status_lines(self.loop, st)
                 if "fix label was declined" in x}
        self.assertEqual(still, {3, 4})              # still labelled; unreadable left for later

    def test_an_enqueue_failure_is_held_with_the_base_it_failed_at(self):
        def boom(*a, **k):
            raise OSError("boom")
        notices = []
        with mock.patch.object(gate, "enqueue_isolated", boom), \
                mock.patch.object(self.module, "_fix_notice",
                                  lambda loop, n, base, outcome, kind: notices.append(
                                      (n, base, outcome, kind))):
            log = self.run_gate(self.labeled(label="Agent-Fix"))
        self.assertIn("fix held: isolated worker unavailable: OSError: boom", log)
        self.assertNotIn("UnboundLocalError", log)
        self.assertEqual(notices, [(12, BASE, "held — isolated worker unavailable: OSError: boom",
                                    "held")])

    def feed(self):
        """Switch the loop's observer feed on and capture what would be POSTed: the messages."""
        self.loop = {**self.loop, "observer": {"route": "widgets-observe", "profile": "default",
                                               "deliver": "telegram"}}
        sent = []

        def post(loop, block, tag, delivery="", urgent=False):
            sent.append(block["message"])
            return True, "", False
        patch = mock.patch.object(observer, "_post", post)
        patch.start()
        self.addCleanup(patch.stop)
        return sent

    def test_a_held_hand_off_does_not_swallow_the_queued_one_at_the_same_base(self):
        # #1080 / #1057: both notices were keyed (loop, issue, base, "fixing", base), so the
        # "queued" notice after a "held" one at the same base was reported as already recorded.
        sent = self.feed()

        def boom(*a, **k):
            raise OSError("boom")
        with mock.patch.object(gate, "enqueue_isolated", boom):
            self.run_gate(self.labeled())                   # the worker is down: held at BASE
        self.run_gate(self.labeled())                       # it is back: queued at the same BASE
        self.assertEqual(len(sent), 2, sent)
        self.assertIn("held — isolated worker unavailable: OSError: boom", sent[0])
        self.assertIn(f"fix turn queued from main at {BASE[:7]}", sent[1])

    def test_nobody_else_and_nothing_else_starts_a_fix(self):
        log = self.run_gate(self.labeled(sender="owner"))       # the author is not a maintainer
        self.assertIn("not in triage.maintainers", log)
        self.run_gate(self.labeled(label="bug"))
        self.live = issue(author="drive-by")
        self.run_gate(self.labeled())
        self.live = issue(labels=())                            # removed again since
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])

    def test_an_open_pr_that_already_fixes_the_issue_skips_it_with_one_comment(self):
        self.open_prs = [{"number": 3, "body": "Fixes #120"}, {"number": 4, "body": "see #12"},
                         {"number": 5, "body": "Intro\n\nCloses #12."}]
        log = self.run_gate(self.labeled())
        self.assertIn("PR #5 already fixes it", log)
        self.assertEqual(self.enqueued, [])
        self.assertEqual(len(self.posted), 1)
        path, body, login = self.posted[0]
        self.assertTrue(path.endswith("/issues/12/comments"))
        self.assertEqual(body, {"body": "PR #5 already fixes this; not handing it to the fixer."})
        self.assertEqual(login, "review")

    def test_unrelated_open_prs_do_not_block_and_an_unreadable_listing_holds(self):
        self.open_prs = [{"number": 3, "body": "Fixes #120"}, {"number": 4, "body": None}]
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(self.posted, [])
        self.enqueued.clear()
        with mock.patch.object(gh, "open_prs_read", lambda loop: (None, "boom")):
            log = self.run_gate(self.labeled())
        self.assertIn("unreadable", log)
        self.assertEqual(self.enqueued, [])

    def test_a_failed_wait_comment_is_retried_on_a_later_sweep(self):
        self.origin = 7
        real = gh.api
        with mock.patch.object(
                gh, "api", lambda loop, path, method="GET", body=None, login=None:
                None if method == "POST" else real(loop, path, method, body, login)):
            self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.assertEqual((st.fix_holds(), st.fix_hold_said(12), self.posted), ({12: 7}, False, []))
        fix_hold.sweep(self.loop, st)                   # GitHub is back: the comment is retried
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(st.fix_hold_said(12))
        fix_hold.sweep(self.loop, st)
        self.assertEqual(len(self.posted), 1)

    def test_a_later_hold_on_the_same_issue_says_so_again_after_a_drop(self):
        self.origin = 7
        self.run_gate(self.labeled())                   # hold: one wait comment
        st = state_mod.state_for(self.loop)
        self.assertEqual((st.fix_holds(), len(self.posted), st.fix_hold_said(12)), ({12: 7}, 1, True))
        self.origin = None                              # re-labelled with no origin: hold dropped
        self.run_gate(self.labeled())
        self.assertEqual((st.fix_holds(), st.fix_hold_said(12)), ({}, False))
        self.enqueued.clear()
        self.origin, self.origin_pr = 9, {"number": 9, "state": "open", "merged": False}
        self.run_gate(self.labeled())                   # a new origin PR: hold again
        self.assertEqual(st.fix_holds(), {12: 9})
        waits = [b for p, b, _l in self.posted if p.endswith("/issues/12/comments")
                 and "waiting for #" in b["body"]]
        self.assertEqual(len(waits), 2)
        self.assertIn("#9", waits[1]["body"])

    def test_the_sweep_dropping_a_closed_pr_hold_clears_the_said_flag(self):
        st = state_mod.state_for(self.loop)
        fix_hold.hold(self.loop, st, 12, 7)
        self.assertTrue(st.fix_hold_said(12))
        self.origin_pr = {"number": 7, "state": "closed", "merged": False}
        fix_hold.sweep(self.loop, st)
        self.assertEqual((st.fix_holds(), st.fix_hold_said(12)), ({}, False))

    def test_a_finding_from_an_open_pr_is_held_until_it_merges(self):
        self.origin = 7
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        st = state_mod.state_for(self.loop)
        self.assertEqual(st.fix_holds(), {12: 7})
        self.assertEqual([b["body"] for _p, b, _l in self.posted],
                         ["waiting for #7 to merge — this finding is about code only on that branch"])
        fix_hold.sweep(self.loop, st)                   # still open: no second comment, no turn
        self.run_gate(self.labeled())
        self.assertEqual((len(self.posted), self.enqueued), (1, []))
        # the PR merges: the watchdog sweep queues the fix from the base and clears the hold
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        self.assertEqual(fix_hold.sweep(self.loop, st), [])
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])
        self.assertEqual(st.fix_holds(), {})

    def test_the_sweep_release_notice_reaches_the_observer_with_its_queued_kind(self):
        # Review of #1873, F1: ``_release`` sends a ``fixing`` notice, and ``key_for`` refuses one
        # without its kind, which ``notify`` catches and logs: the notice vanishes without a
        # sound. Only a configured feed and a captured POST can see that.
        sent = self.feed()
        self.origin = 7
        self.run_gate(self.labeled())                   # held behind PR #7: the "held" notice
        st = state_mod.state_for(self.loop)
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, st)                   # merged: the release queues the fix
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])
        queued = [m for m in sent if "fix turn queued" in m]
        self.assertEqual(len(queued), 1, sent)
        self.assertIn(f"PR #7 merged; fix turn queued from main at {BASE[:7]}", queued[0])

    def test_a_release_that_scheduled_nothing_sends_no_queued_notice(self):
        sent = self.feed()
        self.origin = 7
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        with mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: "duplicate running"):
            fix_hold.sweep(self.loop, st)               # a turn is already running: no new one
        self.assertEqual(st.fix_holds(), {})
        self.assertEqual([m for m in sent if "fix turn queued" in m], [], sent)

    def test_a_still_open_origin_stays_held_in_the_sweep(self):
        self.origin = 7
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {12: 7}))

    def test_an_origin_closed_unmerged_drops_the_hand_off_with_a_comment(self):
        self.origin = 7
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.origin_pr = {"number": 7, "state": "closed", "merged": False}
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {}))
        self.assertEqual(len(self.posted), 2)           # the wait comment, then the moot one
        self.assertIn("closed without merging", self.posted[1][1]["body"])
        # labelled again with the origin already closed: comment, no turn
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, len(self.posted)), ([], 3))

    def test_an_unreadable_origin_is_retried_by_the_sweep_never_guessed(self):
        self.origin = 7
        self.origin_pr = None
        log = self.run_gate(self.labeled())
        self.assertIn("unreadable", log)
        st = state_mod.state_for(self.loop)
        self.assertEqual((self.enqueued, self.posted, st.fix_holds()), ([], [], {12: 7}))
        fix_hold.sweep(self.loop, st)                   # still unreadable
        self.assertEqual((self.enqueued, self.posted, st.fix_holds()), ([], [], {12: 7}))
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, st)
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(st.fix_holds(), {})

    def test_a_human_filed_issue_is_never_held(self):
        self.origin = None
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual((self.posted, state_mod.state_for(self.loop).fix_holds()), ([], {}))

    def test_a_declared_dependency_is_held_and_the_comment_quotes_it(self):
        self.live = issue(body="The docs half. Follow-up to PR #7, which adds the flag.")
        self.open_prs = [{"number": 7, "body": ""}]
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {12: 7}))
        self.assertEqual([b["body"] for _p, b, _l in self.posted],
                         ["waiting for #7 to merge — this issue says “Follow-up to PR #7”. "
                          "Re-apply the fix label once that no longer holds."])

    def test_a_mentioned_pr_is_not_waited_for(self):
        # #686: "PR #684" named as an example held #685, the fix #684 itself needed.
        self.live = issue(body="Live on PR #7 (the Desktop page): the reviewer can't run Node.")
        self.open_prs = [{"number": 7, "body": ""}]
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual((self.posted, state_mod.state_for(self.loop).fix_holds()), ([], {}))

    def test_re_applying_the_label_releases_a_hold_that_no_longer_applies(self):
        self.live = issue(body="Depends on #7.")
        self.open_prs = [{"number": 7, "body": ""}]
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.assertEqual(st.fix_holds(), {12: 7})
        self.live = issue(body="No longer waits on anything.")      # the person edited the issue
        self.run_gate(self.labeled())
        self.assertEqual((len(self.enqueued), st.fix_holds()), (1, {}))
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, st)                                # no second turn on merge
        self.assertEqual(len(self.enqueued), 1)

    def test_a_declared_dependency_closed_unmerged_says_so_plainly(self):
        self.live = issue(body="Blocked by #7.")
        self.open_prs = [{"number": 7, "body": ""}]
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.origin_pr = {"number": 7, "state": "closed", "merged": False}
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {}))
        self.assertIn("which this issue says it depends on, closed without merging",
                      self.posted[-1][1]["body"])
        self.assertNotIn("moot", self.posted[-1][1]["body"])

    def test_an_origin_already_merged_queues_at_once(self):
        self.origin = 7
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)

    def test_without_unattended_pushes_the_label_is_named_not_acted_on(self):
        self.loop = {**self.loop, "unattended_fixer_push": False}
        log = self.run_gate(self.labeled())
        self.assertIn("fixer-push", log)
        self.assertEqual(self.enqueued, [])


class Worker(Base):
    def setUp(self):
        super().setUp()
        self.live = issue()
        patch = mock.patch.object(gh, "api", side_effect=lambda *a, **k: self.live)
        patch.start()
        self.addCleanup(patch.stop)
        comments = mock.patch.object(gh, "issue_comments_read", return_value=([], ""))
        comments.start()
        self.addCleanup(comments.stop)

    def test_the_prompt_names_the_base_branch_and_carries_the_issue_as_data(self):
        text = turn_prompts.issue_fix_prompt(self.loop, {"repo": REPO, "pr": 12, "head": BASE})
        self.assertIn("diaktoros/issue-12", text)
        self.assertIn(BASE, text)
        self.assertIn("issue_comment", text)
        self.assertLess(text.index("What to do"), text.index("Typo in README."))
        # An issue may come from a model that read an untrusted diff (#247): a work order to
        # verify against the code, not a fact to implement.
        self.assertIn("treat it as a work order, not as a fact", text)
        self.assertIn("Verify its claim against the code", text)

    def test_a_recorded_fix_is_write_evidence(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "diaktoros/issue-12")
        with sup._connect() as con:
            self.assertEqual(run_supervisor.write_records(con, run_id), "issue fix recorded")
        with self.assertRaisesRegex(ValueError, "already recorded"):
            sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "diaktoros/issue-12")

    def test_tools_advertise_only_the_issue_fixers_two_writes(self):
        tools = trusted_turn.tool_instructions("issue_fixer")
        self.assertIn("broker_client open_pr", tools)
        self.assertIn("broker_client issue_comment", tools)
        for other in ("broker_client review", "broker_client push", "ruling", "triage"):
            self.assertNotIn(other, tools)


class BrokerWrite(Base):
    def setUp(self):
        super().setUp()
        self.sup, self.run_id = self.fix_row()
        self.live = issue()
        self.calls = []

        def api(loop, path, method="GET", body=None, login=None):
            self.calls.append((path, method, body, login))
            if path == "/user":
                return {"login": login, "id": sum(map(ord, login))}   # one account per login (C2)
            if path == f"/repos/{REPO}/pulls" and method == "POST":
                return {"number": 40, "head": {"ref": body["head"]}}
            if method == "POST":
                return {"number": 40, "id": 9}
            return self.live
        self.pushed = []
        self.real_open_branch = safe_push.open_branch

        def open_branch(loop, **kw):
            self.pushed.append(kw)
            return {"new_head": "d" * 40, "login": "fix"}
        for target, name, value in ((gh, "api", mock.Mock(side_effect=api)),
                                    (config, "by_repo", mock.Mock(side_effect=lambda r: self.loop)),
                                    (safe_push, "open_branch", mock.Mock(side_effect=open_branch))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        scope = broker_ipc.RunScope(REPO, 12, BASE, "issue_fixer", "diaktoros/issue-12",
                                    self.run_id, str(self.db))
        self.server = broker_ipc.RunBroker(self.loop, scope, self.root)

    def manifest(self):
        import base64
        import hashlib
        data = b"fixed\n"
        return {"base_head": BASE, "message": "Fix the typo",
                "files": [{"path": "README.md", "content_b64": base64.b64encode(data).decode(),
                           "sha256": hashlib.sha256(data).hexdigest()}]}

    def open_pr(self, **change):
        return json.dumps({"operation": "open_pr", "manifest": self.manifest(),
                           "title": "Fix the README typo", "body": "Fixed; checked the render.",
                           **change}).encode()

    def posts(self):
        return [(path, body) for path, method, body, _ in self.calls if method == "POST"]

    def test_open_pr_pushes_a_fresh_branch_opens_the_pr_and_requests_the_reviewer(self):
        self.server._dispatch(self.open_pr())
        self.assertEqual(self.pushed[0]["branch"], "diaktoros/issue-12")
        self.assertEqual(self.pushed[0]["base"], BASE)
        pr, request = self.posts()
        self.assertEqual(pr[0], f"/repos/{REPO}/pulls")
        self.assertEqual((pr[1]["head"], pr[1]["base"]), ("diaktoros/issue-12", "main"))
        self.assertIn("Fixes #12", pr[1]["body"])
        self.assertEqual(request, (f"/repos/{REPO}/pulls/40/requested_reviewers",
                                   {"reviewers": ["review"]}))
        result = self.sup.issue_fix_result(self.run_id)
        self.assertEqual((result["state"], result["pr_number"]), ("requested", 40))
        self.assertTrue(self.server.completed)
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "already used"):
            self.server._dispatch(json.dumps({"operation": "issue_comment",
                                              "body": "x"}).encode())

    def test_bad_requests_are_refused_before_the_write_is_spent(self):
        for raw in (self.open_pr(title=""), self.open_pr(title="two\nlines"),
                    self.open_pr(body=""), self.open_pr(manifest={"base_head": BASE}),
                    json.dumps({"operation": "push", "manifest": self.manifest()}).encode(),
                    json.dumps({"operation": "review", "verdict": "APPROVE",
                                "body": "x"}).encode()):
            with self.subTest(raw=raw[:60]), self.assertRaises(
                    (broker_ipc.ProtocolError, broker.BrokerDenied)):
                self.server._dispatch(raw)
        self.assertEqual((self.pushed, self.posts()), ([], []))
        self.assertIsNone(self.sup.issue_fix_result(self.run_id))
        # Refused requests still count (#144): the agent tried, so its exit is not retried.
        self.assertEqual(self.server.requests, 6)

    def test_turning_pushes_off_stops_the_write_before_the_record(self):
        self.loop = {**self.loop, "unattended_fixer_push": False}
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "not enabled"):
            self.server._dispatch(self.open_pr())
        self.assertIsNone(self.sup.issue_fix_result(self.run_id))

    def run_row(self):
        with ledger.connect(self.db) as con:
            return tuple(con.execute("SELECT state, error FROM runs WHERE id=?",
                                     (self.run_id,)).fetchone())

    def test_an_unknown_push_outcome_is_uncertain_and_opens_no_pr(self):
        safe_push.open_branch.side_effect = safe_push.PushFailure("unknown")
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "unknown"):
            self.server._dispatch(self.open_pr())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.posts(), [])
        # #522: the run itself is held uncertain, so `status` lists it and `reconcile` applies;
        # it never ends `succeeded` with its write unknown.
        self.assertEqual(self.run_row(), ("uncertain", "post-write push quarantine: unknown"))

    def test_a_published_push_is_reported_as_published_and_opens_no_pr(self):
        # #824 on the issue-fix seat (shadow review of #989): the transport errored but the host
        # read the branch back at the commit. The seat is told it published, never "unknown".
        safe_push.open_branch.side_effect = safe_push.PushFailure("published")
        with self.assertRaises(broker_ipc.ProtocolError) as said:
            self.server._dispatch(self.open_pr())
        self.assertIn("push published", str(said.exception))
        self.assertNotIn("the push's outcome is unknown", str(said.exception))
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.run_row(), ("uncertain", "post-write push quarantine: published"))

    def test_an_oserror_in_the_reconciled_audit_holds_the_run_uncertain(self):
        self.reconciled_audit_fails(OSError("disk full"))

    def test_a_denied_reconciled_audit_holds_the_run_uncertain(self):
        self.reconciled_audit_fails(broker.BrokerDenied("durable audit directory unavailable"))

    def reconciled_audit_fails(self, exc):
        """#769: the real open_branch, driven by the broker's issue-fix write."""
        new = "e" * 40

        def cas(loop, repo, branch, base, files, message, login, identity, *, before_push, **kw):
            before_push(new)
        if True:
            if True:
                sup, run_id, server = self.sup, self.run_id, self.server

                def audit(loop, record):
                    if record.get("phase") == "reconciled":
                        raise exc
                safe_push.open_branch.side_effect = self.real_open_branch
                with mock.patch.object(broker, "authorize_issue_fix", return_value="fix"), \
                        mock.patch.object(safe_push, "_api", return_value={"login": "fix", "id": 3}), \
                        mock.patch.object(safe_push, "_git_cas", side_effect=cas), \
                        mock.patch.object(safe_push, "_audit", side_effect=audit), \
                        mock.patch.object(safe_push, "_confirm_ref", return_value=new), \
                        mock.patch.object(safe_push.gh, "fetch", return_value=(None, "HTTP 404 Not Found")), \
                        self.assertRaisesRegex(broker_ipc.ProtocolError, "unknown"):
                    server._dispatch(self.open_pr())
                self.assertEqual(sup.issue_fix_result(run_id)["state"], "uncertain")
                self.assertEqual(self.posts(), [])
                with ledger.connect(self.db) as con:
                    row = tuple(con.execute("SELECT state, error FROM runs WHERE id=?",
                                            (run_id,)).fetchone())
                self.assertEqual(row, ("uncertain", "post-write push quarantine: unknown"))

    def test_an_unknown_pr_create_holds_the_run_uncertain(self):
        def api(loop, path, method="GET", body=None, login=None):
            if path == f"/repos/{REPO}/pulls" and method == "POST":
                raise OSError("lost response")
            return ({"login": login, "id": sum(map(ord, login))} if path == "/user"   # (C2)
                    else self.live)
        gh.api.side_effect = api
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "PR could not be confirmed"):
            self.server._dispatch(self.open_pr())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.run_row(),
                         ("uncertain", "post-write push quarantine: pr_create_unknown"))

    def test_could_not_fix_is_one_signed_comment_on_the_issue(self):
        self.server._dispatch(json.dumps({"operation": "issue_comment",
                                          "body": "Needs a decision on the API."}).encode())
        (path, body), = self.posts()
        self.assertEqual(path, f"/repos/{REPO}/issues/12/comments")
        self.assertIn("Automated by", body["body"])
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "posted")
        self.assertEqual(self.pushed, [])

    def test_a_label_removed_since_denies_the_comment(self):
        self.live = issue(labels=())
        with self.assertRaises(broker.BrokerDenied):
            self.server._dispatch(json.dumps({"operation": "issue_comment",
                                              "body": "x"}).encode())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "denied")
        self.assertEqual(self.posts(), [])


class WorkerLaunch(Base):
    """The real ``_run_production`` for an issue-fix row, with only the outside world faked: the
    seat's model resolves through the real ``resolve_seat`` (the profile lookup is the fake), so a
    seat name the resolver does not know fails here exactly as it would in production."""

    def test_an_issue_fix_turn_resolves_the_fixers_model_and_launches(self):
        sup, run_id = self.fix_row(state="launching")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
        resolved, launched = [], []
        inference = mock.Mock(upstream="https://api.example/v1/chat/completions", key="k",
                              model="m", api_mode="chat_completions", proxy_model="",
                              client_identity="", provider="custom", profile="coder")
        inference.credential_provider.return_value = None

        def resolve_profile(profile, seat, settings):
            resolved.append((profile, seat))
            return inference

        def run_turn(loop, scope, **kw):
            launched.append((scope.role, scope.branch, scope.head))
            return 0
        sup.production_config = self.root / "runtime.json"
        with mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_profile",
                                  side_effect=resolve_profile), \
                mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=issue()), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
                mock.patch.object(sup, "recover"), \
                mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            sup._run_production(run_id, "w")
        with sup._connect() as con:
            row = con.execute("SELECT state,error FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertNotIn("seat model unresolved", row["error"] or "", dict(row))
        self.assertEqual(resolved, [("coder", "fixer")])       # the fixer seat's own profile
        self.assertEqual(launched, [("issue_fixer", "diaktoros/issue-12", BASE)])


class OpenBranchGuards(Base):
    """#233: open_branch's own guards, each pinned — the branch name and the fixer's identity."""

    def setUp(self):
        super().setUp()
        import base64
        import hashlib
        data = b"fixed\n"
        self.manifest = {"base_head": BASE, "message": "Fix the typo",
                         "files": [{"path": "README.md",
                                    "content_b64": base64.b64encode(data).decode(),
                                    "sha256": hashlib.sha256(data).hexdigest()}]}
        self.user = {"login": "fix", "id": 3}
        for target, name, value in (
                (broker, "authorize_issue_fix", mock.Mock(return_value="fix")),
                (safe_push, "_api", mock.Mock(side_effect=lambda loop, path, **kw: self.user)),
                (safe_push, "_git_cas", mock.Mock(side_effect=AssertionError("no push")))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, branch="diaktoros/issue-12"):
        return safe_push.open_branch(self.loop, repo=REPO, number=12, base=BASE, branch=branch,
                                     manifest=self.manifest)

    def test_only_the_issues_own_branch_may_be_pushed(self):
        for branch in ("main", "diaktoros/issue-13", "diaktoros/issue-12/x", "fix-7"):
            with self.subTest(branch=branch), self.assertRaisesRegex(broker.BrokerDenied,
                                                                     "unsafe branch ref"):
                self.open(branch)
        safe_push._git_cas.assert_not_called()

    def test_the_token_must_resolve_to_the_fixer_before_anything_is_built(self):
        for user, reason in (({"login": "someone-else", "id": 3}, "fixer identity changed"),
                             ({"login": "fix", "id": 0}, "fixer identity changed"),
                             ({"login": "fix"}, "fixer identity changed")):
            with self.subTest(user=user):
                self.user = user
                with self.assertRaisesRegex(broker.BrokerDenied, reason):
                    self.open()
        safe_push._git_cas.assert_not_called()


class OpenBranchExisting(OpenBranchGuards):
    """An existing diaktoros/issue-N branch is a pre-write denial, not an uncertain push."""

    def read(self, result):
        patch = mock.patch.object(safe_push.gh, "fetch", mock.Mock(return_value=result))
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_existing_branch_is_denied_before_any_write(self):
        self.read(({"ref": "refs/heads/diaktoros/issue-12", "object": {"sha": "a" * 40}}, ""))
        with self.assertRaisesRegex(broker.BrokerDenied,
                                    "diaktoros/issue-12 already exists — inspect it, and see docs/issues.md"):
            self.open()
        safe_push._git_cas.assert_not_called()

    def test_a_failed_read_is_denied_before_any_write(self):
        for result in ((None, "HTTP 500"), (None, "URLError: down"), (None, ""), ([], "")):
            with self.subTest(result=result):
                self.read(result)
                with self.assertRaises(broker.BrokerDenied):
                    self.open()
        safe_push._git_cas.assert_not_called()

    def test_a_404_proceeds_to_the_push(self):
        self.read((None, "HTTP 404 Not Found"))
        with self.assertRaises(broker.BrokerDenied):    # the stubbed push fails; it was reached
            self.open()
        safe_push._git_cas.assert_called_once()


class ConfirmAfterPush(OpenBranchGuards):
    """#522: GitHub may not serve a just-pushed ref at once. The read-back waits it out, so a
    landed issue-fix branch is ``published`` (and its PR opens), not ``unknown``."""

    NEW = "e" * 40

    def setUp(self):
        super().setUp()
        self.reads = []
        self.sleeps = []
        self.visible_after = 2                    # reads that 404 before the ref shows

        def api(loop, path, **kw):
            if path == "/user":
                return self.user
            self.reads.append(path)
            if len(self.reads) <= self.visible_after:
                raise broker.BrokerDenied("GitHub read request failed")      # 404, as _api says
            return {"ref": "refs/heads/diaktoros/issue-12", "object": {"sha": self.NEW}}

        def cas(loop, repo, branch, base, files, message, login, identity, *, before_push,
                **kw):
            before_push(self.NEW)
        for target, name, value in (
                (safe_push, "_api", mock.Mock(side_effect=api)),
                (safe_push, "_git_cas", mock.Mock(side_effect=cas)),
                (safe_push, "_audit", mock.Mock()),
                (safe_push.time, "sleep", mock.Mock(side_effect=self.sleeps.append)),
                (safe_push.gh, "fetch", mock.Mock(return_value=(None, "HTTP 404 Not Found")))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_ref_that_shows_late_is_published(self):
        result = self.open()
        self.assertEqual((result["outcome"], result["new_head"]), ("published", self.NEW))
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.sleeps, list(safe_push.CONFIRM_WAITS[:2]))

    def test_a_ref_that_never_shows_is_unknown_after_the_window(self):
        self.visible_after = 99
        with self.assertRaises(safe_push.PushFailure) as failed:
            self.open()
        self.assertEqual(failed.exception.outcome, "unknown")
        self.assertEqual(self.sleeps, list(safe_push.CONFIRM_WAITS))


    def audit_fails_on_reconcile(self, exc):
        def audit(loop, record):
            if record.get("phase") == "reconciled":
                raise exc
        safe_push._audit.side_effect = audit

    def test_a_failed_reconciled_audit_is_an_unknown_push(self):
        for exc in (OSError("disk full"), broker.BrokerDenied("durable audit directory unavailable")):
            with self.subTest(exc=exc):
                self.audit_fails_on_reconcile(exc)
                with self.assertRaises(safe_push.PushFailure) as failed:
                    self.open()
                self.assertEqual(failed.exception.outcome, "unknown")


class ConfirmRef(unittest.TestCase):
    """The shared read-back (#522): what ends the wait, and what it never waits for."""

    def confirm(self, answers, prior="a" * 40, push_failed=False):
        reads, sleeps = list(answers), []
        with mock.patch.object(safe_push, "_read_ref", side_effect=lambda *a: reads.pop(0)), \
                mock.patch.object(safe_push.time, "sleep", side_effect=sleeps.append):
            observed = safe_push._confirm_ref({}, "/ref", "b", "fix", "n" * 40, prior,
                                              push_failed=push_failed)
        return observed, sleeps

    def test_the_old_head_is_lag_after_a_reported_push(self):
        self.assertEqual(self.confirm(["a" * 40, "n" * 40]), ("n" * 40, [1]))

    def test_another_commit_ends_the_wait_at_once(self):
        self.assertEqual(self.confirm(["c" * 40]), ("c" * 40, []))

    def test_a_refused_push_that_left_the_old_head_is_read_once(self):
        self.assertEqual(self.confirm(["a" * 40], push_failed=True), ("a" * 40, []))

    def test_a_missing_ref_is_waited_out_even_after_a_failed_push(self):
        # A lost response: Git raised, the ref may still have landed.
        self.assertEqual(self.confirm([None, "n" * 40], push_failed=True), ("n" * 40, [1]))


class RealBareBranch(unittest.TestCase):
    """``_git_cas`` with ``from_branch``: one commit on the pinned base, to a branch that must
    not exist yet — real git, local bare repositories."""

    def test_a_fresh_branch_from_the_pinned_base_and_never_over_an_existing_one(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            remote = str(Path(temp) / "remote.git")
            local = str(Path(temp) / "creator.git")

            def git(*args, **kwargs):
                return subprocess.check_output(["git", *args], text=True, **kwargs).strip()
            git("init", "-q", "--bare", "--initial-branch=main", remote)
            git("init", "-q", "--bare", "--initial-branch=main", local)
            blob = git("--git-dir", local, "hash-object", "-w", "--stdin", input="typo")
            tree = git("--git-dir", local, "mktree", input=f"100644 blob {blob}\tREADME.md\n")
            env = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
                   "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}
            pinned = git("--git-dir", local, "commit-tree", tree, input="base\n", env=env)
            later = git("--git-dir", local, "commit-tree", tree, "-p", pinned, input="later\n",
                        env=env)
            stray = git("--git-dir", local, "commit-tree", tree, input="stray\n", env=env)
            git("--git-dir", local, "push", "-q", remote, f"{later}:refs/heads/main")
            git("--git-dir", local, "push", "-q", remote, f"{stray}:refs/heads/elsewhere")
            loop = {"tokens": {"fix": str(Path(temp) / "dummy")}}
            Path(loop["tokens"]["fix"]).write_text("not-a-real-token")
            identity = {"name": "fix", "email": "3+fix@users.noreply.github.com"}

            def cas(head, branch="diaktoros/issue-12", content=b"fixed"):
                return safe_push._git_cas(loop, REPO, branch, head, [("README.md", content)],
                                          "Fix the typo", "fix", identity, remote=remote,
                                          from_branch="main")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1",
                                              "GIT_CONFIG_KEY_0": "protocol.file.allow",
                                              "GIT_CONFIG_VALUE_0": "never",
                                              "GIT_CONFIG_GLOBAL": "/does/not/exist"}):
                new = cas(pinned)              # main moved on since: the pinned commit still counts
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/diaktoros/issue-12"), new)
                self.assertEqual(git("--git-dir", remote, "rev-list", "--parents", "-n", "1", new),
                                 f"{new} {pinned}")
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/main"), later)
                with self.assertRaises(broker.BrokerDenied):
                    cas(pinned, content=b"another fix")   # the branch exists: never overwritten
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/diaktoros/issue-12"), new)
                with self.assertRaisesRegex(broker.BrokerDenied, "not on the base branch"):
                    cas(stray, branch="diaktoros/issue-13")


if __name__ == "__main__":
    unittest.main()
