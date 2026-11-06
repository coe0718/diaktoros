"""#1864: the findings an adjudicator rules valid but not blocking become issues.

With ``adjudicator_file_issues`` on, the ruling turn may ask its broker to ``file_issue``: checked
exactly as the reviewer's would be, recorded (``shadow_issues``, under the adjudicator's run), never
posted by the seat, and never spending the one ruling. The watchdog sweep files each once the run
has ruled, as the reader, labelled ``adjudicator-found``, as a follow-up to the PR (its
``filed_issues`` row, so ``fix_hold`` waits for the PR), unless the reviewer or a shadow filed it.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_partial_view_no_approve as fixture  # noqa: E402
from diaktoros import broker, broker_ipc, config, gh, ledger, ruling_issues  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.run_supervisor import Supervisor, turn_prompts  # noqa: E402

REPO, HEAD = fixture.REPO, fixture.HEAD
TRIAGE = {"route": "r", "labels": ["bug", "P3"], "fix_label": "agent-fix", "authors": ["a"]}


class Setting(unittest.TestCase):
    def test_off_unless_set_and_carried_by_the_form(self):
        self.assertFalse(config.adjudicator_file_issues({}))
        self.assertFalse(config.adjudicator_file_issues({"adjudicator_file_issues": "yes"}))
        self.assertTrue(config.adjudicator_file_issues({"adjudicator_file_issues": True}))
        self.assertFalse(config.DEFAULTS["adjudicator_file_issues"])
        out = settings_form.apply_settings({}, {"adjudicator_file_issues": True})
        self.assertIs(out["adjudicator_file_issues"], True)

    def test_the_prompt_asks_for_them_only_where_the_loop_files_them(self):
        loop = {"triage": TRIAGE}
        self.assertEqual(turn_prompts.ruling_issues_section(loop), "")
        text = turn_prompts.ruling_issues_section({**loop, "adjudicator_file_issues": True})
        self.assertIn("broker_client file_issue", text)
        self.assertIn("on an ACCEPT every such finding you let through", text)
        self.assertIn("`bug`, `P3`", text)
        self.assertEqual(turn_prompts._standing_section({"adjudicator_file_issues": True},
                                                        "reviewer"), "")


class Record(unittest.TestCase):
    # The broker fixture's plumbing, borrowed (inheriting would rerun its tests here).
    setUp = fixture.Broker.setUp
    api = fixture.Broker.api
    start = fixture.Broker.start

    def adjudicator_run(self):
        sup = Supervisor(self.root / "runs.sqlite")
        sup.enqueue("a", REPO, 7, HEAD, "adjudicator")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', owner='w', launch_intent=?",
                        (time.time(),))
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        return sup, broker_ipc.RunScope(REPO, 7, HEAD, "adjudicator", "fix-7", run_id,
                                        str(sup.db), "g")

    def send(self, server, payload):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(json.dumps(payload).encode() + b"\n")
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def ask(self, server, title="Retry never ends", labels=("bug",)):
        return self.send(server, {"operation": "file_issue", "title": title,
                                  "body": "evidence at a.py:1", "labels": list(labels)})

    def test_recorded_never_posted_checked_like_the_reviewers_and_the_ruling_still_stands(self):
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE, "adjudicator_file_issues": True}
        sup, scope = self.adjudicator_run()
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(broker_ipc, "_deliver_ruling"), \
                mock.patch("diaktoros.merge_check.refresh_quietly"):
            server = self.start(scope)
            answer = self.ask(server)
            self.assertEqual((answer["ok"], answer.get("result")), (True, {"accepted": True}),
                             answer)
            self.assertFalse(self.ask(server, title="Other", labels=("agent-fix",))["ok"])
            self.assertFalse(self.ask(server, title="Priority only", labels=("P3",))["ok"])
            ruled = self.send(server, {"operation": "ruling", "verdict": "ACCEPT",
                                       "body": "the rest does not block"})
            self.assertTrue(ruled["ok"], ruled)                # filing spent nothing
        self.assertEqual(self.posts, [])                       # nothing reached GitHub
        [row] = sup.shadow_issues_waiting(REPO, seat="adjudicator")
        self.assertEqual((row["title"], json.loads(row["labels"]), row["verdict"]),
                         ("Retry never ends", ["bug"], "ACCEPT"))
        self.assertEqual(sup.shadow_issues_waiting(REPO), [])  # not a shadow's

    def test_a_shadows_request_the_loop_never_files_gives_way(self):
        # Review of #1874, F1, through the broker: it reads shadow.file_issues from the loop.
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE, "adjudicator_file_issues": True,
                     "shadow": {"profile": "p"}}                      # shadow filing off
        sup, scope = self.adjudicator_run()
        sup.enqueue("s", REPO, 7, HEAD, "shadow")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', launch_intent=1 WHERE seat='shadow'")
            shadow = con.execute("SELECT id FROM runs WHERE seat='shadow'").fetchone()[0]
        sup.record_shadow_issue(shadow, REPO, 7, HEAD, "Retry never ends", "b", ["bug"], 3)
        for files_on, accepted in ((True, False), (False, True)):
            with self.subTest(shadow_file_issues=files_on):
                loop = {**self.loop, "shadow": {"profile": "p", "file_issues": files_on}}
                with mock.patch.object(config, "by_repo", return_value=loop):
                    answer = self.ask(self.start(scope))
                self.assertEqual(answer["ok"], accepted, answer)

    def test_refused_where_the_loop_does_not_file_them(self):
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE}
        sup, scope = self.adjudicator_run()
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            answer = self.ask(self.start(scope))
        self.assertFalse(answer["ok"])
        self.assertIn("adjudicator_file_issues is off", answer["error"])
        self.assertEqual(sup.shadow_issues_waiting(REPO, seat="adjudicator"), [])

    def test_an_adjudicator_never_reaches_the_reviewers_posting_path(self):
        from diaktoros import broker_issues
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE, "adjudicator_file_issues": True}
        _sup, scope = self.adjudicator_run()
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "out of scope"):
            broker_issues.IssueWrites._file_issue(server, b"{}", {"operation": "file_issue"})


class EarlierRequests(unittest.TestCase):
    """Review of #1874, F1: one title per PR, but an earlier request that never will be filed
    gives way to the adjudicator's, while one filed, in flight or still due keeps blocking."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sup = Supervisor(Path(tmp.name) / "runs.sqlite")
        self.n = 0

    def run_of(self, seat, state="running", verdict=None):
        self.n += 1
        run, now = f"{seat[0]}{self.n}", time.time()
        with ledger.connect(self.sup.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated,launch_intent) VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",
                        (run, f"d{self.n}", REPO, 7, HEAD, seat, state, f"t{self.n}", now, now,
                         now))
            if verdict:
                con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,"
                            "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                            (run, REPO, 7, HEAD, f"t{self.n}", verdict, "why", now, now))
        return run

    def earlier(self, seat, state, run_state="succeeded", verdict=None):
        run = self.run_of(seat, "running")
        self.sup.record_shadow_issue(run, REPO, 7, HEAD, "Retry never ends", "b", ["bug"], 3,
                                     seat=seat)
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE shadow_issues SET state=? WHERE run_id=?", (state, run))
            con.execute("UPDATE runs SET state=? WHERE id=?", (run_state, run))
            if verdict:
                con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,"
                            "created,updated) VALUES(?,?,?,?,'t',?,'why',1,1)",
                            (run, REPO, 7, HEAD, verdict))

    def ask(self, shadow_files=True):
        run = self.run_of("adjudicator")
        self.sup.record_shadow_issue(run, REPO, 7, HEAD, "retry never ends", "b", ["bug"], 3,
                                     seat="adjudicator", shadow_files=shadow_files)
        return run

    def test_one_that_never_will_be_filed_gives_way(self):
        for name, args, shadow_files in (
                ("denied", ("shadow", "denied"), True),
                ("a shadow's, shadow filing off", ("shadow", "recorded"), False),
                ("an ended adjudicator run that never ruled", ("adjudicator", "recorded"), True)):
            with self.subTest(name):
                self.setUp()
                self.earlier(*args)
                run = self.ask(shadow_files)
                [row] = self.sup.shadow_issues_waiting(REPO, seat="adjudicator")
                self.assertEqual(row["run_id"], run)

    def test_one_filed_in_flight_or_still_due_keeps_blocking(self):
        for name, args, shadow_files in (
                ("posted", ("shadow", "posted"), True),
                ("posting", ("shadow", "posting"), True),
                ("uncertain", ("shadow", "uncertain"), True),
                ("a duplicate of a filed one", ("shadow", "duplicate"), True),
                ("a shadow's, still due", ("shadow", "recorded"), True),
                ("a live adjudicator run's", ("adjudicator", "recorded", "running"), True),
                ("a ruled adjudicator run's", ("adjudicator", "recorded", "succeeded", "ACCEPT"),
                 True)):
            with self.subTest(name):
                self.setUp()
                self.earlier(*args)
                with self.assertRaisesRegex(ValueError, "already asked for"):
                    self.ask(shadow_files)

    def test_a_live_shadow_keeps_its_sequence_and_cap_after_giving_way(self):
        # Review of #1874, F3: deleted, the shadow's first request left a hole its next one fell
        # into (COUNT + 1 reused sequence 2: an IntegrityError, nothing recorded).
        shadow = self.run_of("shadow")
        for title in ("Retry never ends", "Lock leaks"):
            self.sup.record_shadow_issue(shadow, REPO, 7, HEAD, title, "b", ["bug"], 3)
        self.ask(shadow_files=False)                           # takes the first one's title
        self.assertEqual(self.sup.record_shadow_issue(shadow, REPO, 7, HEAD, "Docs gap", "b",
                                                      ["bug"], 3), 3)
        with self.assertRaisesRegex(ValueError, "at most 3"):  # its cap still counts the first
            self.sup.record_shadow_issue(shadow, REPO, 7, HEAD, "Cache drops case", "b",
                                         ["bug"], 3)
        with ledger.connect(self.sup.db) as con:
            gave = con.execute("SELECT state, error FROM shadow_issues WHERE run_id=? AND seq=1",
                               (shadow,)).fetchone()
        self.assertEqual(gave[0], "denied")
        self.assertIn("gave way to adjudicator run", gave[1])

    def test_a_shadow_still_cannot_reask_a_denied_title(self):
        self.earlier("shadow", "denied")
        with self.assertRaisesRegex(ValueError, "already asked for"):
            self.sup.record_shadow_issue(self.run_of("shadow"), REPO, 7, HEAD,
                                         "Retry never ends", "b", ["bug"], 3)


class FilePending(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.loop = {"id": "one", "repo": REPO, "read_token": "reader",
                     "adjudicator_file_issues": True}
        self.n = 0

    def recorded(self, title, verdict="ACCEPT", run_state="succeeded"):
        """An adjudicator run's recorded follow-up, and its ruling (``verdict`` None: none)."""
        self.n += 1
        run, now = f"a{self.n}", time.time()
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                        (run, f"d{self.n}", REPO, 7, HEAD, "adjudicator", run_state,
                         f"t{self.n}", now, now))
            if verdict:
                con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,"
                            "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                            (run, REPO, 7, HEAD, f"t{self.n}", verdict, "why", now, now))
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES(?,1,?,?,?,?,?,?,?,'recorded',?,?)",
                        (run, REPO, 7, HEAD, title, " ".join(title.casefold().split()),
                         "evidence at a.py:1", json.dumps(["bug"]), now, now))
        return run

    def reviewer_filed(self, title):
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('r','dr',?,7,?,'reviewer','succeeded',0,'',1,1)",
                        (REPO, HEAD))
            con.execute("INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,"
                        "state,created,updated) VALUES('r',1,?,7,?,?,?,1,'posted',1,1)",
                        (REPO, HEAD, title, " ".join(title.casefold().split())))

    def file(self):
        posts = []

        def write(loop, path, method, body, login):
            posts.append((path, body, login))
            return {"number": 200 + len(posts), "labels": [{"name": x} for x in body["labels"]]}, ""
        pr = {"number": 7, "head": {"sha": HEAD, "ref": "fix-7"}}
        with mock.patch.object(gh, "pr", return_value=pr), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            return ruling_issues.file_pending(self.loop, db=self.db), posts

    def state(self, run):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT state FROM shadow_issues WHERE run_id=?",
                               (run,)).fetchone()[0]

    def test_an_accepts_two_follow_ups_are_filed_labelled_and_held_for_the_pr(self):
        first = self.recorded("Retry never ends")
        with ledger.connect(self.db) as con:                   # a second one in the same run
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES(?,2,?,7,?,'Lock leaks','lock leaks',"
                        "'evidence','[\"bug\"]','recorded',?,?)",
                        (first, REPO, HEAD, time.time(), time.time()))
        filed, posts = self.file()
        self.assertEqual(filed, [201, 202])
        for path, body, login in posts:
            self.assertEqual((path, login), (f"/repos/{REPO}/issues", "reader"))
            self.assertEqual(body["labels"], ["bug", "adjudicator-found"])
            self.assertTrue(body["body"].startswith("Follow-up to #7: a finding the adjudicator "
                                                    "ruled valid but not blocking, in its ACCEPT "
                                                    "ruling"), body["body"])
            self.assertIn("Filed by the Diaktoros host for an adjudicator ruling from PR #7",
                          body["body"])
        for issue in filed:                        # fix_hold waits for #7; lineage counted
            self.assertEqual(self.sup.filed_origin_pr(REPO, issue), 7)
            self.assertEqual(self.sup.filed_depth(REPO, issue), 1)
        self.assertEqual(self.file(), ([], []))                # never twice

    def test_a_reject_files_the_ones_it_asked_for(self):
        self.recorded("Unrelated docs gap", verdict="REJECT")
        filed, posts = self.file()
        self.assertEqual(len(filed), 1)
        self.assertIn("in its REJECT ruling", posts[0][1]["body"])

    def test_one_the_reviewer_filed_is_not_filed_twice(self):
        self.reviewer_filed("Retry never ends")
        run = self.recorded("retry never ends.")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "duplicate")

    def test_nothing_before_the_ruling_and_dropped_without_one(self):
        ruling = self.recorded("Not ruled yet", verdict=None, run_state="running")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(ruling), "recorded")       # still ruling: waits
        ended = self.recorded("Never ruled", verdict=None, run_state="failed")
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(ended), "denied")

    def test_off_files_nothing(self):
        run = self.recorded("Retry never ends")
        self.loop["adjudicator_file_issues"] = False
        self.assertEqual(self.file(), ([], []))
        self.assertEqual(self.state(run), "recorded")

    def test_the_daily_bound_holds(self):
        runs = [self.recorded(title) for title in ("Docs miss the flag", "Retry loops forever",
                                                     "Lock is never freed", "Cache key drops case")]
        with mock.patch.object(config, "ADJUDICATOR_ISSUES_DAILY", 3):
            filed, _ = self.file()
            self.assertEqual(len(filed), 3)
            self.assertEqual(self.file(), ([], []))            # the fourth waits for room
        self.assertEqual(self.state(runs[3]), "recorded")

    def test_a_full_daily_bound_still_drops_and_logs_a_later_unruled_follow_up(self):
        # Review of #1982, F1: the bound stops the POSTs, not the sweep. A follow-up behind ones
        # it holds back, from a run that ended without a ruling, is still denied and logged.
        first = self.recorded("Docs miss the flag")
        held = self.recorded("Retry loops forever")
        lost = self.recorded("Never ruled", verdict=None, run_state="failed")
        logged = []
        pr = {"number": 7, "head": {"sha": HEAD, "ref": "fix-7"}}
        with mock.patch.object(config, "ADJUDICATOR_ISSUES_DAILY", 1), \
                mock.patch.object(gh, "pr", return_value=pr), \
                mock.patch.object(broker, "_write", return_value=(
                    {"number": 201, "labels": [{"name": "bug"}, {"name": "adjudicator-found"}]},
                    "")) as write, \
                mock.patch.object(broker, "_audit"):
            filed = ruling_issues.file_pending(self.loop, db=self.db, log=logged.append)
        self.assertEqual((filed, write.call_count), ([201], 1))     # the bound: one POST only
        self.assertEqual([self.state(first), self.state(held), self.state(lost)],
                         ["posted", "recorded", "denied"])
        self.assertIn("adjudicator-found issue for #7 ('Never ruled') denied: the adjudicator's "
                      "run ended without a ruling", logged)


class EveryPath(unittest.TestCase):
    """init (form and flag), set, show and setup (#323), with test_shadow.Cli's harness."""
    import test_shadow as _shadow
    setUpClass = classmethod(_shadow.Cli.setUpClass.__func__)
    tearDownClass = classmethod(_shadow.Cli.tearDownClass.__func__)
    setUp, cli, init = _shadow.Cli.setUp, _shadow.Cli.cli, _shadow.Cli.init

    def value(self):
        return json.loads(self.file.read_text()).get("adjudicator_file_issues")

    def test_the_switch_on_every_path(self):
        for extra, settings, want in (((), None, False),
                                      ((), {"adjudicator_file_issues": True}, True),
                                      (("--adjudicator-file-issues", "off"),
                                       {"adjudicator_file_issues": True}, False)):
            with self.subTest(extra=extra, settings=settings):
                self.file.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual((rc, self.value()), (0, want), out)
        loop_id = self.file.stem
        rc, out = self.cli("set", "--loop", loop_id, "--adjudicator-file-issues", "on")
        self.assertEqual((rc, self.value()), (0, True), out)
        rc, out = self.cli("show", "--loop", loop_id)
        self.assertIn(" true ", next(line for line in out.splitlines()
                                   if line.strip().startswith("adjudicator_file_issues")))
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--adjudicator-file-issues", "on"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, loop_id, interactive=False)
        self.assertIn("--adjudicator-file-issues=on", argv)

import test_shadow_escalate_ruling as _ruling  # noqa: E402


class Sweep(_ruling.Base):
    """Review of #1874, F2: the real watchdog sweep files a ruled follow-up."""
    sweep = _ruling.CallSites.sweep

    def test_the_watchdog_sweep_files_a_ruled_follow_up(self):
        import sqlite3
        from contextlib import closing
        self.loop = {**self.loop, "adjudicator_file_issues": True,
                     "read_token": self.loop.get("read_token") or "reader"}
        now = time.time()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('adj','dadj',?,7,?,'adjudicator','succeeded',0,"
                        "'breach:1',?,?)", (REPO_SWEEP, HEAD_SWEEP, now, now))
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES('adj',?,7,?,'breach:1','ACCEPT','why',?,?)",
                        (REPO_SWEEP, HEAD_SWEEP, now, now))
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES('adj',1,?,7,?,'Lock leaks',"
                        "'lock leaks','evidence','[]','recorded',?,?)",
                        (REPO_SWEEP, HEAD_SWEEP, now, now))
        posts = []

        def write(loop, path, method, body, login):
            posts.append((path, body))
            return {"number": 300, "labels": [{"name": x} for x in body["labels"]]}, ""
        with mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            self.sweep()                     # the first sweep only snapshots the heads
            self.sweep()
        issues = [body for path, body in posts if path == f"/repos/{REPO_SWEEP}/issues"]
        self.assertEqual(len(issues), 1, posts)
        self.assertEqual(issues[0]["labels"], ["adjudicator-found"])
        self.assertTrue(issues[0]["body"].startswith("Follow-up to #7"))

    def test_the_watchdog_sweep_logs_a_follow_up_it_drops(self):
        # #1359: a filing that will never be made is said in the sweep's log, not only filed ones.
        import sqlite3
        from contextlib import closing
        from scripts import watchdog
        self.loop = {**self.loop, "adjudicator_file_issues": True}
        now = time.time()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('adj','dadj',?,7,?,'adjudicator','failed',0,"
                        "'breach:1',?,?)", (REPO_SWEEP, HEAD_SWEEP, now, now))
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES('adj',1,?,7,?,'Lock leaks',"
                        "'lock leaks','evidence','[]','recorded',?,?)",
                        (REPO_SWEEP, HEAD_SWEEP, now, now))
        logged = []
        with mock.patch.object(watchdog, "log", side_effect=logged.append), \
                mock.patch.object(broker, "_write") as write, \
                mock.patch.object(broker, "_audit"):
            self.sweep()                     # the first sweep only snapshots the heads
            self.sweep()
        write.assert_not_called()
        self.assertTrue(any("adjudicator-found issue for #7 ('Lock leaks') denied: the "
                            "adjudicator's run ended without a ruling" in line
                            for line in logged), logged)


REPO_SWEEP, HEAD_SWEEP = _ruling.fg.REPO, _ruling.fg.HEAD

if __name__ == "__main__":
    unittest.main()
