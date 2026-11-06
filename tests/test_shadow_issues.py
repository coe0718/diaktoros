"""#914: the issues a shadow review asks for are filed, attributed as the shadow's.

The shadow's broker is no-write: its ``file_issue`` is recorded (``shadow_issues``), checked exactly as
the reviewer's would be, and the seat gets the same "accepted". The watchdog sweep files each one
once the real review exists at that head, unless the reviewer filed the same finding there, as the
reader, labelled ``shadow-found``, naming the model. Off unless ``shadow.file_issues``; at most
``shadow.issues_daily`` (``config.shadow_issues_daily``) a day.
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
from diaktoros.config import blocks  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO, HEAD = fixture.REPO, fixture.HEAD
TRIAGE = {"route": "r", "labels": ["bug", "P3"], "fix_label": "agent-fix", "authors": ["a"]}
APPROVED = [{"id": 9, "state": "APPROVED", "commit_id": HEAD, "submitted_at": "2026-01-01T00:00:00Z",
             "user": {"login": "review"}}]


class Setting(unittest.TestCase):
    def test_file_issues_is_a_bool_and_off_by_default(self):
        self.assertEqual(blocks.normalize_shadow({"profile": "p", "file_issues": True}, "x"),
                         {"profile": "p", "file_issues": True})
        with self.assertRaisesRegex(config.ConfigError, "file_issues must be true or false"):
            blocks.normalize_shadow({"profile": "p", "file_issues": "yes"}, "x")
        self.assertFalse(config.shadow_file_issues({"shadow": {"profile": "p"}}))
        self.assertTrue(config.shadow_file_issues({"shadow": {"profile": "p", "file_issues": True}}))

    def test_the_cli_and_form_turn_it_on_and_off_and_need_a_profile(self):
        on = settings_form.shadow_block({"profile": "p"}, None, None, "set", file_issues="on")
        self.assertTrue(on["file_issues"])
        off = settings_form.shadow_block(on, None, None, "set", file_issues="off")
        self.assertNotIn("file_issues", off)
        with self.assertRaisesRegex(config.ConfigError, "needs a shadow profile"):
            settings_form.shadow_block({}, None, None, "set", file_issues="on")


class IssuesDailySetting(unittest.TestCase):
    """``shadow.issues_daily`` (#1515): bounds like review_only_daily, 0 = no cap, default 25."""

    def test_read_with_the_default_and_normalized_from_the_loop_file(self):
        self.assertEqual(config.shadow_issues_daily({}), 25)
        self.assertEqual(config.shadow_issues_daily({"shadow": {"profile": "p"}}), 25)
        for value in (0, 1, 40, 1000):
            with self.subTest(value=value):
                shadow = blocks.normalize_shadow({"profile": "p", "issues_daily": value}, "x")
                self.assertEqual(shadow, {"profile": "p", "issues_daily": value})
                self.assertEqual(config.shadow_issues_daily({"shadow": shadow}), value)

    def test_out_of_range_and_non_whole_numbers_are_refused(self):
        for value in (-1, 1001, 2.5, "7", True):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(config.ConfigError, "issues_daily must be a whole number"):
                blocks.normalize_shadow({"profile": "p", "issues_daily": value}, "x")

    def test_the_cli_and_form_set_it_and_need_a_profile(self):
        block = settings_form.shadow_block({"profile": "p"}, None, None, "set", issues_daily="0")
        self.assertEqual(block["issues_daily"], 0)
        block = settings_form.shadow_block(block, None, None, "set", issues_daily=" 40 ")
        self.assertEqual(block["issues_daily"], 40)
        for bad in ("1001", "-1", "many"):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "issues_daily"):
                settings_form.shadow_block({"profile": "p"}, None, None, "set", issues_daily=bad)
        with self.assertRaisesRegex(config.ConfigError, "needs a shadow profile"):
            settings_form.shadow_block({}, None, None, "set", issues_daily="5")

    def test_the_form_overlay_carries_it(self):
        raw = {"shadow": {"profile": "p"}}
        out = settings_form.apply_settings(dict(raw), {"shadow_issues_daily": "12"})
        self.assertEqual(out["shadow"], {"profile": "p", "issues_daily": 12})
        self.assertIn("shadow_issues_daily", settings_form.SETTINGS_SCHEMA)


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

    def test_a_shadow_filing_without_a_type_label_is_refused_and_not_recorded(self):
        """The rule docs/configuration.md and `--label --help` now state (#1402)."""
        self.loop = {**self.loop, "id": "one", "triage": TRIAGE}
        sup, scope = self.shadow_run()
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            server = self.start(scope, no_write=True)
            refused = self.ask(server, title="Priority only", labels=("P3",))
        self.assertFalse(refused["ok"])
        self.assertIn("type label", refused["error"])
        self.assertEqual(sup.shadow_issues_waiting(REPO), [])


class TypeLabelRuleIsDocumented(unittest.TestCase):
    """The two places a reader meets the rule say it (#1402)."""

    def test_the_shadow_issues_paragraph_names_the_type_label_rule(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "configuration.md").read_text()
        start = text.index("**Filing its issues (`shadow.file_issues`")
        self.assertIn("type label", text[start:text.index("**Who files it:**", start)])

    def test_the_label_flag_help_names_the_type_label_rule(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run([sys.executable, "-m", "diaktoros.broker_client", "--help"],
                             cwd=root, capture_output=True, text=True, check=True).stdout
        self.assertIn("type label", " ".join(out.split()))


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

    def file(self, reviews=APPROVED, now=None, branch="fix-7", labelled=None, logged=None,
             audit=None):
        """One sweep against a fake GitHub. It keeps the labels a new issue is created with, as
        GitHub does for a token with push access; with ``labelled`` it drops them (#1589), and
        the labels endpoint answers with ``labelled(names)``: the issue's labels, or a refusal.
        ``audit``: what stands in for the broker's audit writer (default: a no-op mock)."""
        posts = []

        def write(loop, path, method, body, login):
            posts.append((path, body, login))
            if path.endswith("/labels"):
                return labelled(body["labels"])
            kept = body["labels"] if labelled is None else []
            return {"number": 100 + len(posts), "labels": [{"name": x} for x in kept]}, ""
        pr = None if branch is None else {"number": 7, "head": {"sha": HEAD, "ref": branch}}
        with mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(gh, "pr", return_value=pr), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit", **({} if audit is None else audit)):
            filed = shadow_issues.file_pending(self.loop, db=self.db, now=now,
                                               log=(logged if logged is not None else []).append)
        return filed, posts

    def error(self, run):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT error FROM shadow_issues WHERE run_id=?",
                               (run,)).fetchone()[0]

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

    def test_its_lineage_is_recorded_as_the_reviewers_is(self):
        # Tuck upheld the shadow on #1343: a shadow-found issue needs its filed_issues row, so
        # the origin-PR hold (#324) and the lineage bound (#487) see it.
        self.recorded("A finding")
        filed, posts = self.file()
        self.assertEqual(self.sup.filed_origin_pr(REPO, filed[0]), 7)
        self.assertEqual(self.sup.filed_depth(REPO, filed[0]), 1)
        self.assertIn("Filed by the Diaktoros host for a shadow review from PR #7", posts[0][1]["body"])
        self.assertIn("Lineage depth 1.", posts[0][1]["body"])

    def test_on_an_issue_fix_branch_it_is_one_deeper(self):
        with ledger.connect(self.db) as con:             # PR #7 fixes issue 40, itself depth 2
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES('o','do',?,3,?,'reviewer','succeeded',0,'',1,1)",
                        (REPO, HEAD))
            con.execute("INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,"
                        "state,issue_number,created,updated) VALUES('o',1,?,3,?,'t','t',2,"
                        "'posted',40,1,1)", (REPO, HEAD))
        self.recorded("A finding")
        filed, _ = self.file(branch="diaktoros/issue-40")
        self.assertEqual(self.sup.filed_depth(REPO, filed[0]), 3)

    def test_an_unreadable_pr_files_nothing_this_sweep(self):
        run = self.recorded("A finding")
        self.assertEqual(self.file(branch=None)[1], [])
        self.assertEqual(self.state(run), "recorded")

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
        logged = []
        self.assertEqual(self.file(reviews=[], logged=logged)[1], [])
        self.assertEqual(self.state(run), "denied")
        # #1359: a lost filing is said in the sweep's log, once.
        self.assertEqual(logged, ["shadow-found issue for #7 ('A finding') denied: "
                                  "the real review never reached its head"])
        self.file(reviews=[], logged=logged)
        self.assertEqual(len(logged), 1)

    def test_an_uncertain_post_is_said_in_the_sweeps_log(self):
        # #1359: a POST whose outcome is unknown is never replayed, so it is logged, with why.
        run = self.recorded("A finding")
        logged = []
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", return_value=(None, "HTTP 502")):
            shadow_issues.file_pending(self.loop, db=self.db, log=logged.append)
        self.assertEqual(self.state(run), "uncertain")
        [line] = logged
        self.assertTrue(line.startswith("shadow-found issue for #7 ('A finding') uncertain: "
                                        "POST outcome unknown"), line)

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

    TOPICS = ["Docs miss the flag", "Retry loops forever", "Lock is never freed",
              "Wrong exit code on refusal", "Cache key ignores head", "Timeout not budgeted",
              "Label list unsorted", "Stale branch is kept", "Hook payload is unchecked",
              "Ledger row lacks index"]

    def states(self):
        with ledger.connect(self.db) as con:
            return [r[0] for r in con.execute("SELECT state FROM shadow_issues ORDER BY run_id")]

    def test_a_loop_without_the_key_gets_the_default_cap(self):
        self.assertEqual(config.SHADOW_ISSUES_DAILY, 25)
        self.assertEqual(config.shadow_issues_daily(self.loop), config.SHADOW_ISSUES_DAILY)
        for topic in self.TOPICS:
            self.recorded(topic)                               # distinct findings
        filed, _posts = self.file()
        self.assertEqual(len(filed), len(self.TOPICS))         # under the default: all of them

    def test_the_loops_cap_stops_the_sweep_and_leaves_the_rest_recorded(self):
        # The loop's value, not the constant (25): fails if file_pending reads the constant.
        self.loop["shadow"] = {**self.loop["shadow"], "issues_daily": 3}
        for topic in self.TOPICS:
            self.recorded(topic)
        filed, _posts = self.file()
        self.assertEqual(len(filed), 3)
        self.assertEqual(sorted(self.states()), ["posted"] * 3 + ["recorded"] * 7)
        self.assertEqual(self.file()[0], [])                   # the cap holds for the day
        self.assertEqual(len(self.file(now=time.time() + 86400 + 60)[0]), 3)   # window rolled

    def test_a_cap_of_zero_files_every_waiting_issue(self):
        self.loop["shadow"] = {**self.loop["shadow"], "issues_daily": 0}
        words = ["zebra", "quartz", "lantern", "midnight", "cobalt", "harbor", "velvet", "thistle",
                 "orchid", "granite", "falcon", "juniper", "saffron", "tundra", "walnut", "ember",
                 "pelican", "marble", "cypress", "dragonfly", "obsidian", "nutmeg", "badger",
                 "sapphire", "koala", "trellis", "vermilion", "windmill"]
        for word in words:                                     # more than the default cap
            self.recorded(f"{word} " * 3)
        self.assertGreater(len(words), config.SHADOW_ISSUES_DAILY)
        filed, _posts = self.file()
        self.assertEqual(len(filed), len(words))
        self.assertEqual(set(self.states()), {"posted"})

    def test_a_cap_above_the_old_constant_is_honoured(self):
        self.loop["shadow"] = {**self.loop["shadow"], "issues_daily": 8}
        for topic in self.TOPICS:
            self.recorded(topic)
        self.assertEqual(len(self.file()[0]), 8)

    def test_labels_github_drops_at_creation_are_added_after_it(self):
        # #1589: GitHub drops create-time labels, silently, for a token without push access.
        run = self.recorded("A finding")
        filed, posts = self.file(labelled=lambda names: ([{"name": x} for x in names], ""))
        self.assertEqual(filed, [101])
        self.assertEqual([(path, body.get("labels")) for path, body, _login in posts],
                         [(f"/repos/{REPO}/issues", ["bug", "shadow-found"]),
                          (f"/repos/{REPO}/issues/101/labels", ["bug", "shadow-found"])])
        self.assertEqual(posts[1][2], "reader")
        self.assertEqual((self.state(run), self.error(run)), ("posted", None))

    def test_labels_that_still_do_not_land_are_reported_never_refiled(self):
        run = self.recorded("A finding")
        logged = []
        refused = (None, "HTTP 403: Resource not accessible")
        filed, posts = self.file(labelled=lambda _names: refused, logged=logged)
        self.assertEqual((filed, len(posts)), ([101], 2))
        self.assertEqual(self.state(run), "unlabelled")         # filed, not fully: never again
        self.assertIn("labels: GitHub did not apply bug, shadow-found (HTTP 403", self.error(run))
        self.assertIn("the reader reader needs permission to label issues", self.error(run))
        [line] = logged
        self.assertIn("#101 filed without its labels", line)
        self.assertEqual(self.file()[1], [])

    def test_a_label_write_that_raises_is_a_gap_not_an_uncertain_filing(self):
        run = self.recorded("A finding")
        def boom(_names):
            raise TimeoutError("read timed out")
        self.assertEqual(self.file(labelled=boom)[0], [101])
        self.assertEqual(self.state(run), "unlabelled")
        self.assertIn("(TimeoutError: read timed out)", self.error(run))

    def audited(self, labelled):
        """The operations the real broker audit records for one filing (#1622 review, F1)."""
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.loop = {**self.loop, "state_dir": tmp.name}
        self.file(labelled=labelled, audit={"wraps": broker._audit})
        lines = (Path(tmp.name) / "broker-audit.jsonl").read_text().splitlines()
        return [json.loads(line)["operation"] for line in lines]

    def test_a_label_gap_is_in_the_audit_record(self):
        self.recorded("A finding")
        self.assertEqual(self.audited(lambda _names: (None, "HTTP 403")),
                         ["file_shadow_issue", "shadow_issue_labels_missing"])

    def test_labels_added_after_the_post_are_audited_as_added(self):
        self.recorded("A finding")
        self.assertEqual(self.audited(lambda names: ([{"name": x} for x in names], "")),
                         ["file_shadow_issue", "shadow_issue_labels"])

    def test_an_audit_failure_after_the_labels_landed_keeps_the_filed_issue(self):
        # #1622 review, F3: GitHub confirmed the issue and its labels; the audit line can't be
        # written. The issue's number is kept, and it is never filed again.
        run = self.recorded("A finding")
        failing = {"side_effect": [None, OSError("audit disk full")]}
        filed, _posts = self.file(labelled=lambda names: ([{"name": x} for x in names], ""),
                                  audit=failing)
        self.assertEqual(filed, [101])
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT state, issue_number FROM shadow_issues WHERE "
                                         "run_id=?", (run,)).fetchone()[:], ("posted", 101))
        self.assertEqual(self.file()[1], [])

    def test_an_unlabelled_filing_counts_toward_the_daily_cap(self):
        self.loop = {**self.loop, "shadow": {**self.loop["shadow"], "issues_daily": 1}}
        self.recorded("A finding")
        self.assertEqual(self.file(labelled=lambda _names: (None, "HTTP 403"))[0], [101])
        self.recorded("Another, unrelated topic entirely")
        self.assertEqual(self.file()[0], [])                     # the day's one is spent

    def test_a_label_the_endpoint_leaves_off_is_named(self):
        run = self.recorded("A finding")
        self.file(labelled=lambda names: ([{"name": x} for x in names if x != "shadow-found"], ""))
        self.assertIn("did not apply shadow-found (the label write left them off)", self.error(run))
        self.assertNotIn("bug", self.error(run).split("(")[0])

    def test_a_failed_post_is_uncertain_and_never_replayed(self):
        run = self.recorded("A finding")
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", return_value=(None, "HTTP 502")):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "uncertain")
        self.assertEqual(self.file()[1], [])

    def test_an_earlier_calls_unsent_flag_never_makes_a_failed_post_a_retry(self):
        # A missing credential on an earlier call in this thread leaves gh's "never sent" flag
        # set. This POST went out and failed (a 502): its outcome is unknown, so it is
        # uncertain and never replayed, whatever the earlier call left behind.
        run = self.recorded("A finding")
        gh._LAST.unsent = True
        self.addCleanup(setattr, gh._LAST, "unsent", False)
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", return_value=(None, "HTTP 502")):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "uncertain")

    def test_a_denial_before_the_post_stays_recorded_and_is_filed_once_fixed(self):
        run = self.recorded("A finding")
        self.loop["read_token"] = ""
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "recorded")
        self.assertIn("no reader identity", self.error(run))
        self.assertEqual(self.sup.filed_titles(REPO, 7), [])
        self.loop["read_token"] = "reader"
        filed, posts = self.file()
        self.assertEqual(len(filed), 1)
        self.assertEqual(self.state(run), "posted")

    @staticmethod
    def answered(status, text=""):
        """A ``broker._write`` stand-in for GitHub answering with ``status``: gh records the
        response's own status (``gh.last_status``) as the real request path does."""
        def write(*args, **kwargs):
            gh._LAST.status = status
            return None, f"HTTP {status}: {text}" if text else f"HTTP {status}"
        return write

    def test_githubs_4xx_refusal_stays_recorded_and_a_5xx_is_uncertain(self):
        for status, state in ((403, "recorded"), (502, "uncertain")):
            run = self.recorded(f"A finding {state}")
            with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                    mock.patch.object(gh, "pr",
                                      return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                    mock.patch.object(broker, "_write", side_effect=self.answered(status)):
                self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
            self.assertEqual(self.state(run), state)

    def test_a_4xx_refusal_is_retried_and_filed_after_the_permission_is_fixed(self):
        run = self.recorded("A finding")
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write",
                                  side_effect=self.answered(403, "Resource not accessible")):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "recorded")
        filed, _ = self.file()
        self.assertEqual(len(filed), 1)
        self.assertEqual(self.state(run), "posted")

    def sweep_with(self, write):
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch.object(broker, "_write", side_effect=write), \
                mock.patch.object(broker, "_audit"):
            return shadow_issues.file_pending(self.loop, db=self.db)

    def test_a_missing_reader_credential_is_not_sent_and_is_filed_once_it_exists(self):
        # F1: the credential lookup fails before any request is built, through the real gh path.
        import tempfile
        run = self.recorded("A finding")
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        token = Path(home.name) / "reader.pat"
        self.loop["tokens"] = {"reader": str(token)}               # mapped, file not there yet
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "recorded")
        self.assertIn("not sent", self.error(run))
        self.assertIn("no token file", self.error(run))
        token.write_text("secret\n")
        filed, _ = self.file()
        self.assertEqual(len(filed), 1)
        self.assertEqual(self.state(run), "posted")

    def test_a_timeout_after_the_request_left_is_not_treated_as_unsent(self):
        # A call that failed after the token was read must stay unknown, never retried.
        import tempfile
        run = self.recorded("A finding")
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        token = Path(home.name) / "reader.pat"
        token.write_text("secret\n")
        self.loop["tokens"] = {"reader": str(token)}
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch("diaktoros.config.host_guards.guard_network"), \
                mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        self.assertEqual(self.state(run), "uncertain")
        self.assertEqual(self.file()[1], [])                       # never replayed

    def post_through_gh(self, title, urlopen_effect):
        """One sweep through the real gh request path, ``urlopen`` playing the network."""
        import tempfile
        run = self.recorded(title)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        token = Path(home.name) / "reader.pat"
        token.write_text("secret\n")
        self.loop["tokens"] = {"reader": str(token)}
        with mock.patch.object(gh, "reviews", return_value=APPROVED), \
                mock.patch.object(gh, "pr", return_value={"number": 7, "head": {"ref": "fix-7"}}), \
                mock.patch("diaktoros.config.host_guards.guard_network"), \
                mock.patch("urllib.request.urlopen", side_effect=urlopen_effect):
            self.assertEqual(shadow_issues.file_pending(self.loop, db=self.db), [])
        return run

    def test_a_transport_failure_that_mentions_a_status_is_unknown_and_never_replayed(self):
        # F4: the exception text reads like a 403, but no HTTP answer arrived: the POST may have
        # been applied, so it is uncertain and a later sweep never sends it again.
        run = self.post_through_gh(
            "A finding", TimeoutError("timed out after HTTP 403 proxy message"))
        self.assertEqual(self.state(run), "uncertain")
        self.assertEqual(self.file()[1], [])                       # never replayed

    def test_a_real_403_answer_through_gh_is_not_sent_and_retried(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {},
                                     io.BytesIO(b'{"message": "Resource not accessible"}'))
        run = self.post_through_gh("A finding", err)
        self.assertEqual(self.state(run), "recorded")
        self.assertEqual(len(self.file()[0]), 1)
        self.assertEqual(self.state(run), "posted")

    def test_the_broker_refuses_a_repository_mismatch_and_no_reader_as_not_sent(self):
        args = dict(number=7, head=HEAD, title="t", body="b", labels=[], model="m", depth=1)
        with self.assertRaisesRegex(broker.BrokerNotSent, "repository mismatch"):
            broker.file_shadow_issue(self.loop, repo="other/repo", **args)
        with self.assertRaisesRegex(broker.BrokerNotSent, "no reader identity"):
            broker.file_shadow_issue({**self.loop, "read_token": ""}, repo=REPO, **args)

    def test_a_408_is_uncertain_and_never_replayed(self):
        run = self.recorded("A finding")
        self.assertEqual(self.sweep_with(self.answered(408, "timeout")), [])
        self.assertEqual(self.state(run), "uncertain")
        self.assertEqual(self.file()[1], [])

    def test_a_refusal_takes_no_room_under_the_daily_cap(self):
        # F3: cap of 1; the first finding is refused (nothing written), the second still files.
        self.loop["shadow"] = {**self.loop["shadow"], "issues_daily": 1}
        now = time.time()
        first = self.recorded("Docs miss the flag", at=now - 20)
        second = self.recorded("Retry loops forever", at=now - 10)

        def write(loop, path, method, body, login):
            if body["title"] == "Docs miss the flag":
                return self.answered(422, "Validation Failed")()
            return {"number": 201, "labels": [{"name": x} for x in body["labels"]]}, ""
        self.assertEqual(self.sweep_with(write), [201])
        self.assertEqual((self.state(first), self.state(second)), ("recorded", "posted"))

    def test_a_row_refused_for_ever_is_dropped_after_max_attempts_and_not_re_posted(self):
        # #1732: the real review is at the head, so the WAIT_S drop never ran for these rows.
        run = self.recorded("A finding", at=time.time() - 40 * 86400)
        refused = self.answered(403, "Resource not accessible by integration")
        posts = []

        def write(*args, **kwargs):
            posts.append(args)
            return refused()
        for sweep in range(shadow_issues.MAX_ATTEMPTS - 1):
            self.assertEqual(self.sweep_with(write), [])
            self.assertEqual(self.state(run), "recorded", sweep)
        self.assertEqual(self.sweep_with(write), [])
        self.assertEqual(self.state(run), "denied")
        self.assertIn("dropped after", self.error(run))
        self.assertIn("Resource not accessible", self.error(run))
        sent = len(posts)
        self.assertEqual(sent, shadow_issues.MAX_ATTEMPTS)
        self.sweep_with(write)
        self.sweep_with(write)
        self.assertEqual(len(posts), sent)                    # a denied row is never re-POSTed
        self.assertEqual(self.sup.shadow_issues_waiting(REPO), [])

    def test_the_attempts_of_one_row_do_not_count_against_another(self):
        first = self.recorded("First finding")
        second = self.recorded("Second finding")

        def write(loop, path, method, body, login):
            if body["title"] == "First finding":
                return self.answered(403, "no")()
            return {"number": 301, "labels": [{"name": x} for x in body["labels"]]}, ""
        self.assertEqual(self.sweep_with(write), [301])
        self.assertEqual((self.state(first), self.state(second)), ("recorded", "posted"))
        with ledger.connect(self.db) as con:
            self.assertEqual([r[0] for r in con.execute(
                "SELECT attempts FROM shadow_issues ORDER BY run_id")], [1, 0])

    def test_an_older_ledger_gets_the_attempts_column(self):
        import sqlite3
        from diaktoros.run_supervisor import ledger_schema
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(ledger_schema.SCHEMA)
        ledger_schema.migrate(con)
        self.assertIsNone(ledger_schema.ledger_problem(con))
        con.execute("ALTER TABLE shadow_issues DROP COLUMN attempts")
        # The worker's check refuses a ledger otherwise fully migrated but without the column.
        self.assertEqual(ledger_schema.ledger_problem(con), "shadow_issues lacks attempts")
        ledger_schema.migrate(con)
        self.assertIn("attempts", {r[1] for r in con.execute("PRAGMA table_info(shadow_issues)")})
        self.assertIsNone(ledger_schema.ledger_problem(con))

    def test_a_release_leaves_no_sequence_a_later_filing_collides_with(self):
        # F2: seq 1 released while seq 2 of the same run stands; the next filing must not take 2.
        run = self.recorded("A finding")
        first = self.sup.record_shadow_filing(run, REPO, 7, HEAD, "Ghost one", 1)
        second = self.sup.record_shadow_filing(run, REPO, 7, HEAD, "Ghost two", 1)
        self.sup.filed_issue_status(run, second, "posted", issue_number=300)
        self.sup.release_shadow_filing(run, first)
        self.assertEqual(self.sup.record_shadow_filing(run, REPO, 7, HEAD, "Ghost three", 1), 3)
        filed, _ = self.file()                                # through the sweep too
        self.assertEqual(len(filed), 1)
        self.assertEqual(self.state(run), "posted")


import test_shadow_escalate_ruling as ruling  # noqa: E402


class Sweep(ruling.Base):
    sweep = ruling.CallSites.sweep          # the real sweep, borrowed

    def test_the_watchdog_sweep_files_them(self):
        with mock.patch.object(shadow_issues, "file_pending", return_value=[]) as file_pending:
            self.sweep()                     # the first sweep only snapshots the heads
            self.sweep()
        self.assertTrue(any(call.args[0].get("repo") == self.loop["repo"]
                            for call in file_pending.call_args_list))
        # #1589: a filing that lost its labels is said in the sweep's log.
        self.assertTrue(all(callable(call.kwargs.get("log"))
                            for call in file_pending.call_args_list))


if __name__ == "__main__":
    unittest.main()
