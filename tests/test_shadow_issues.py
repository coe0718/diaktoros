"""#914: the issues a shadow review asks for are filed, attributed as the shadow's.

The shadow's broker is no-write: its ``file_issue`` is recorded (``shadow_issues``), checked exactly as
the reviewer's would be, and the seat gets the same "accepted". The watchdog sweep files each one
once the real review exists at that head, unless the reviewer filed the same finding there, as the
reader, labelled ``shadow-found``, naming the model. Off unless ``shadow.file_issues``; at most
``config.SHADOW_ISSUES_DAILY`` a day.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_partial_view_no_approve as fixture  # noqa: E402
from diaktoros import broker, config, gh, ledger, shadow_issues  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO, HEAD = fixture.REPO, fixture.HEAD
TRIAGE = {"route": "r", "labels": ["bug", "P3"], "fix_label": "agent-fix", "authors": ["a"]}
APPROVED = [{"id": 9, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-01-01T00:00:00Z",
             "user": {"login": "review"}}]


class Setting(unittest.TestCase):
    def test_file_issues_is_a_bool_and_off_by_default(self):
        self.assertEqual(config.normalize_shadow({"profile": "p", "file_issues": True}, "x"),
                         {"profile": "p", "file_issues": True})
        with self.assertRaisesRegex(config.ConfigError, "file_issues must be true or false"):
            config.normalize_shadow({"profile": "p", "file_issues": "yes"}, "x")
        self.assertFalse(config.shadow_file_issues({"shadow": {"profile": "p"}}))
        self.assertTrue(config.shadow_file_issues({"shadow": {"profile": "p", "file_issues": True}}))

    def test_the_cli_and_form_turn_it_on_and_off_and_need_a_profile(self):
        on = settings_form.shadow_block({"profile": "p"}, None, None, "set", file_issues="on")
        self.assertTrue(on["file_issues"])
        off = settings_form.shadow_block(on, None, None, "set", file_issues="off")
        self.assertNotIn("file_issues", off)
        with self.assertRaisesRegex(config.ConfigError, "needs a shadow profile"):
            settings_form.shadow_block({}, None, None, "set", file_issues="on")


class Record(unittest.TestCase):
    # The no-write broker fixture's plumbing, borrowed (inheriting would rerun its tests here).
    setUp = fixture.Broker.setUp
    api = fixture.Broker.api
    start = fixture.Broker.start

    def shadow_run(self):
        """A live shadow run at the head, and its broker scope."""
        from diaktoros import broker_ipc
        sup = Supervisor(self.root / "runs.sqlite")
        sup.enqueue("s", REPO, 7, HEAD, "shadow")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', owner='w', launch_intent=?", (time.time(),))
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        return sup, broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7", run_id, str(sup.db), "g")

    def ask(self, server, title="Doc says X", labels=("bug",)):
        raw = json.dumps({"operation": "file_issue", "title": title, "body": "evidence at a.py:1",
                          "labels": list(labels)}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def test_a_shadows_file_issue_is_recorded_never_posted_and_checked_like_the_reviewers(self):
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE}
        sup, scope = self.shadow_run()
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            server = self.start(scope, no_write=True)
            answer = self.ask(server)
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(answer["result"], {"accepted": True})
            refused = self.ask(server, title="Other", labels=("agent-fix",))   # the fix label
            self.assertFalse(refused["ok"])
        self.assertEqual(self.posts, [])                      # nothing reached GitHub
        [row] = sup.shadow_issues_waiting(REPO)
        self.assertEqual((row["title"], json.loads(row["labels"]), row["state"]),
                         ("Doc says X", ["bug"], "recorded"))


class FilePending(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.loop = {"id": "one", "repo": REPO, "read_token": "reader", "reviewer_seat": "review",
                     "reviewers": ["review"], "shadow": {"profile": "p", "file_issues": True}}
        self.n = 0

    def recorded(self, title, at=None):
        """A shadow run's recorded issue (its review row gives the model)."""
        self.n += 1
        run = f"s{self.n}"
        now = time.time() if at is None else at
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,?,?,0,?,?,?)",
                        (run, f"d{self.n}", REPO, 7, HEAD, "shadow", "succeeded", f"t{self.n}",
                         now, now))
            con.execute("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,"
                        "authorized,denial,held,created) VALUES(?,?,?,?,?,?,?,1,'',0,?)",
                        (run, REPO, 7, HEAD, "deepseek-x", "APPROVE", "ok", now))
            con.execute("INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,"
                        "labels,state,created,updated) VALUES(?,1,?,?,?,?,?,?,?,'recorded',?,?)",
                        (run, REPO, 7, HEAD, title, " ".join(title.casefold().split()),
                         "evidence", json.dumps(["bug"]), now, now))
        return run

    def reviewer_filed(self, title):
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('r','dr',?,7,?,'reviewer','succeeded',0,'',1,1)",
                        (REPO, HEAD))
            con.execute("INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,"
                        "state,created,updated) VALUES('r',1,?,7,?,?,?,1,'posted',1,1)",
                        (REPO, HEAD, title, " ".join(title.casefold().split())))

    def file(self, reviews=APPROVED, now=None):
        posts = []

        def write(loop, path, method, body, login):
            posts.append((path, body, login))
            return {"number": 100 + len(posts)}, ""
        with mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            filed = shadow_issues.file_pending(self.loop, db=self.db, now=now)
        return filed, posts

    def state(self, run):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT state FROM shadow_issues WHERE run_id=?", (run,)).fetchone()[0]

    def test_filed_once_by_the_reader_labelled_and_attributed_after_the_real_review(self):
        run = self.recorded("Docs say the flag is --x")
        filed, posts = self.file()
        self.assertEqual(filed, [101])
        [(path, body, login)] = posts
        self.assertEqual((path, login), (f"/repos/{REPO}/issues", "reader"))
        self.assertEqual(body["labels"], ["bug", "shadow-found"])
        self.assertIn("Found by a shadow review (`deepseek-x`) of #7", body["body"])
        self.assertEqual(self.state(run), "posted")
        self.assertEqual(self.file()[1], [])                   # never twice

    def test_one_the_reviewer_filed_too_is_not_filed_again(self):
        self.reviewer_filed("Docs say the flag is --x")
        same = self.recorded("docs say the flag is  --x")      # the same, normalized
        close = self.recorded("Docs say the flag is --x.")     # all but identical
        filed, posts = self.file()
        self.assertEqual((filed, posts), ([], []))
        self.assertEqual((self.state(same), self.state(close)), ("duplicate", "duplicate"))

    def test_nothing_before_the_real_review_or_when_off_or_unreadable(self):
        run = self.recorded("A finding")
        self.assertEqual(self.file(reviews=[])[1], [])         # no real review at the head yet
        self.assertEqual(self.state(run), "recorded")          # still waiting
        self.assertEqual(self.file(reviews=None)[1], [])       # unreadable: nothing this sweep
        self.loop["shadow"] = {"profile": "p"}
        self.assertEqual(self.file()[1], [])                   # off
        self.assertEqual(self.state(run), "recorded")

    def test_a_head_the_real_review_never_reaches_is_dropped_after_a_week(self):
        run = self.recorded("A finding", at=time.time() - shadow_issues.WAIT_S - 60)
        self.assertEqual(self.file(reviews=[])[1], [])
        self.assertEqual(self.state(run), "denied")

    def test_an_unreadable_review_never_reads_as_one_that_never_came(self):
        run = self.recorded("A finding", at=time.time() - shadow_issues.WAIT_S - 60)
        self.assertEqual(self.file(reviews=None)[1], [])
        self.assertEqual(self.state(run), "recorded")          # unknown is not "never reached"

    def test_one_another_sweep_claimed_first_is_not_posted(self):
        run = self.recorded("A finding")
        stale = self.sup.shadow_issues_waiting(REPO)           # read before the other claim
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state='posting' WHERE run_id=?", (run,))
        with mock.patch.object(Supervisor, "shadow_issues_waiting", return_value=stale):
            self.assertEqual(self.file()[1], [])

    def test_at_most_the_daily_cap(self):
        for i in range(config.SHADOW_ISSUES_DAILY + 2):
            self.recorded(f"Finding number {i} about something else entirely {i * 7}")
        filed, _posts = self.file()
        self.assertEqual(len(filed), config.SHADOW_ISSUES_DAILY)
        self.assertEqual(self.file()[0], [])                   # the cap holds for the day

    def test_a_failed_post_is_uncertain_and_never_replayed(self):
        run = self.recorded("A finding")
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(broker, "_write", return_value=(None, "HTTP 502")):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "uncertain")
        self.assertEqual(self.file()[1], [])


import test_shadow_escalate_ruling as ruling  # noqa: E402


class Sweep(ruling.Base):
    sweep = ruling.CallSites.sweep          # the real sweep, borrowed

    def test_the_watchdog_sweep_files_them(self):
        with mock.patch.object(shadow_issues, "file_pending", return_value=[]) as file_pending:
            self.sweep()                     # the first sweep only snapshots the heads
            self.sweep()
        self.assertTrue(any(call.args[0].get("repo") == self.loop["repo"]
                            for call in file_pending.call_args_list))


if __name__ == "__main__":
    unittest.main()
