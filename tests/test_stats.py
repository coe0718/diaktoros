"""``stats``: seat turns from the run ledger and, with --github, PRs and reviews.

Real SQLite ledgers (the Supervisor's own schema, and a legacy one without ``finished``); GitHub
is a fake ``gh.api``. The outputs are checked for what they must never carry: a run's error text.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_seat_models as sm  # noqa: E402
from diaktoros import gh, ledger, run_supervisor, stats  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
NOW = 1_800_000_000.0
DAY = 86400.0
SECRET = "/home/someone/.hermes/tokens/rev.pat exploded"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Window(unittest.TestCase):
    def test_since(self):
        self.assertEqual(stats.parse_since("7d", NOW), NOW - 7 * DAY)
        self.assertEqual(stats.parse_since("24h", NOW), NOW - DAY)
        self.assertEqual(stats.parse_since("2026-09-28", NOW),
                         datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp())
        for bad in ("", "7w", "yesterday", "-1d", "2026-13-01"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                stats.parse_since(bad, NOW)

    def test_summary(self):
        self.assertIsNone(stats.summary([]))
        got = stats.summary([10, 20, 30, 40, 1000])
        self.assertEqual((got["n"], got["median"], got["max"], got["p90"]), (5, 30, 1000, 1000))
        self.assertEqual(got["mean"], 220)


class Ledger(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.n = 0

    def row(self, seat, state, created, start=None, finished=None, updated=None, retries=0,
            error=None, repo=REPO):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", repo, self.n, "a" * 40, seat)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, created=?, launch_intent=?, finished=?, "
                        "updated=?, retries=?, error=? WHERE delivery=?",
                        (state, created, start, finished, updated or created, retries, error,
                         f"d{self.n}"))

    def test_turns_by_seat_state_time_and_wait(self):
        since = NOW - 7 * DAY
        self.row("reviewer", "succeeded", NOW - 100, NOW - 90, NOW - 30)        # ran 60 s, waited 10
        self.row("reviewer", "succeeded", NOW - 500, NOW - 400, NOW - 220, retries=1)  # ran 180
        self.row("reviewer", "succeeded", NOW - 900, NOW - 880, None, updated=NOW - 760)  # legacy
        self.row("reviewer", "failed", NOW - 50, NOW - 40, NOW - 20, error=SECRET)
        self.row("reviewer", "waiting", NOW - 10, error="held: waiting for CI on aaaaaaa")
        self.row("issue_fixer", "succeeded", NOW - 300, NOW - 300, NOW - 200)
        self.row("reviewer", "succeeded", since - 1, since, since + 60)          # before the window
        self.row("reviewer", "succeeded", NOW - 100, NOW - 90, NOW - 30, repo="other/repo")
        turns = stats.ledger(self.db, REPO, since)
        reviewer = turns["reviewer"]
        self.assertEqual(reviewer["turns"], 5)
        self.assertEqual(reviewer["states"], {"succeeded": 3, "failed": 1, "waiting": 1})
        self.assertEqual((reviewer["retried"], reviewer["held"]), (1, 1))
        self.assertEqual(reviewer["ran"]["n"], 3)
        self.assertEqual((reviewer["ran"]["median"], reviewer["ran"]["max"]), (120, 180))
        self.assertEqual(reviewer["waited"]["median"], 15)              # 10, 100, 20, 10 s
        self.assertEqual(turns["issue_fixer"]["ran"]["mean"], 100)
        report = {"repo": REPO, "loop": "w", "since": since, "until": NOW, "turns": turns}
        for out in (stats.text(report), stats.as_json(report), stats.as_html(report)):
            self.assertNotIn("exploded", out)                       # never a run's error text
            self.assertNotIn(".hermes", out)
        self.assertIn("2.0 min", stats.text(report))

    def test_no_ledger_and_a_legacy_one(self):
        self.assertIsNone(stats.ledger(self.db.with_name("missing.sqlite"), REPO, 0))
        legacy = self.db.with_name("legacy.sqlite")
        con = sqlite3.connect(legacy)
        con.execute("CREATE TABLE runs (seat TEXT, state TEXT, repo TEXT, created REAL, "
                    "launch_intent REAL, updated REAL, error TEXT)")
        con.execute("INSERT INTO runs VALUES ('reviewer','succeeded',?,?,?,?,NULL)",
                    (REPO, NOW - 100, NOW - 90, NOW - 30))
        con.commit()
        con.close()
        self.assertEqual(stats.ledger(legacy, REPO, 0)["reviewer"]["ran"]["mean"], 60)

    def test_prompt_revision_changes_with_the_template(self):
        from diaktoros import prompts
        before = prompts.revision("reviewer")
        with mock.patch.dict(prompts.ISOLATED, {"reviewer": prompts.ISOLATED["reviewer"] + "x"}):
            self.assertNotEqual(prompts.revision("reviewer"), before)
        self.assertNotEqual(prompts.revision("fixer"), before)

    def test_stats_group_by_prompt_revision_and_model(self):
        since = NOW - 7 * DAY
        self.row("reviewer", "succeeded", NOW - 100, NOW - 90, NOW - 30)       # old row: unknown
        self.row("reviewer", "succeeded", NOW - 200, NOW - 190, NOW - 130)
        self.row("reviewer", "succeeded", NOW - 300, NOW - 290, NOW - 230)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET prompt_rev='aaa', model='m1' WHERE delivery IN ('d2','d3')")
            con.execute("UPDATE runs SET pr=5, head=delivery WHERE delivery IN ('d2','d3')")
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,verdict,"
                        "created) SELECT id,'posted','g',1,'APPROVE',created FROM runs "
                        "WHERE delivery='d2'")
        got = stats.revisions(self.db, REPO, since)
        self.assertEqual(set(got["prompt_rev"]), {"aaa", "unknown"})
        self.assertEqual(got["prompt_rev"]["aaa"]["turns"], 2)
        self.assertEqual(got["prompt_rev"]["aaa"]["verdicts"], {"APPROVE": 1})
        self.assertEqual(got["prompt_rev"]["aaa"]["rounds"]["max"], 2)
        self.assertEqual(got["model"]["unknown"]["turns"], 1)
        report = {"repo": REPO, "loop": "w", "since": since, "until": NOW,
                  "turns": stats.ledger(self.db, REPO, since), "revisions": got}
        self.assertIn("By prompt revision", stats.text(report))
        self.assertIn("m1", stats.text(report))

    def test_stats_group_by_thinking_and_unknown_is_not_off(self):
        since = NOW - 7 * DAY
        for i in range(4):
            self.row("reviewer", "succeeded", NOW - 100 - i, NOW - 90 - i, NOW - 30 - i)
        with ledger.connect(self.db) as con:
            for delivery, level in (("d1", "budget:8000"), ("d2", "off")):
                con.execute("UPDATE runs SET thinking=? WHERE delivery=?", (level, delivery))
        got = stats.revisions(self.db, REPO, since)
        self.assertEqual({k: g["turns"] for k, g in got["thinking"].items()},
                         {"budget:8000": 1, "off": 1, "unknown": 2})
        report = {"repo": REPO, "loop": "w", "since": since, "until": NOW,
                  "turns": stats.ledger(self.db, REPO, since), "revisions": got}
        self.assertIn("By thinking", stats.text(report))
        self.assertIn("budget:8000", stats.text(report))
        legacy = self.db.with_name("legacy3.sqlite")
        con = sqlite3.connect(legacy)
        con.execute("CREATE TABLE runs (seat TEXT, state TEXT, repo TEXT, created REAL, pr INTEGER)")
        con.execute("INSERT INTO runs VALUES ('reviewer','succeeded',?,?,1)", (REPO, NOW - 100))
        con.commit()
        con.close()
        self.assertEqual(stats.revisions(legacy, REPO, 0)["thinking"]["unknown"]["turns"], 1)

    def test_old_ledger_gains_the_thinking_column(self):
        old = self.db.with_name("old2.sqlite")
        Supervisor(old)
        with ledger.connect(old) as con:
            con.execute("ALTER TABLE runs DROP COLUMN thinking")
        Supervisor(old)
        with ledger.connect(old) as con:
            self.assertIn("thinking", {r[1] for r in con.execute("PRAGMA table_info(runs)")})

    def test_an_old_ledger_without_the_columns_reads_as_unknown(self):
        legacy = self.db.with_name("legacy2.sqlite")
        con = sqlite3.connect(legacy)
        con.execute("CREATE TABLE runs (seat TEXT, state TEXT, repo TEXT, created REAL, pr INTEGER)")
        con.execute("INSERT INTO runs VALUES ('reviewer','succeeded',?,?,1)", (REPO, NOW - 100))
        con.commit()
        con.close()
        got = stats.revisions(legacy, REPO, 0)
        self.assertEqual(got["prompt_rev"]["unknown"]["turns"], 1)
        self.assertEqual(got["model"]["unknown"]["turns"], 1)

    def test_old_ledger_is_migrated_additively(self):
        old = self.db.with_name("old.sqlite")
        Supervisor(old)    # current schema
        with ledger.connect(old) as con:
            con.execute("ALTER TABLE runs DROP COLUMN prompt_rev")
            con.execute("ALTER TABLE runs DROP COLUMN model")
        Supervisor(old)
        with ledger.connect(old) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
        self.assertTrue({"prompt_rev", "model", "seat"} <= cols)

    def test_the_ledger_is_opened_read_only(self):
        with mock.patch.object(stats.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            stats.ledger(self.db, REPO, 0)
        self.assertIn("mode=ro", connect.call_args.args[0])


class Finished(sm.Worker):
    def test_a_completed_turn_records_when_it_ended(self):
        before = time.time()
        _, row, _ = self.run_seat("reviewer")
        self.assertEqual(row[0], "succeeded")
        with ledger.connect(self.root / "ledger.sqlite") as con:
            end = con.execute("SELECT finished FROM runs").fetchone()[0]
        self.assertGreaterEqual(end, before)

    def test_a_launched_turn_records_prompt_revision_and_resolved_model(self):
        from diaktoros import prompts
        _, row, _ = self.run_seat("reviewer")
        self.assertEqual(row[0], "succeeded")
        with ledger.connect(self.root / "ledger.sqlite") as con:
            rev, model, seat = con.execute("SELECT prompt_rev, model, seat FROM runs").fetchone()
        self.assertEqual(rev, prompts.revision("reviewer"))
        self.assertRegex(rev, r"^[0-9a-f]{12}$")
        self.assertTrue(model)
        self.assertEqual(seat, "reviewer")

    def test_a_turn_records_the_thinking_level_the_proxy_saw(self):
        def seen(level):
            def turn(kw):
                kw["observed"]["thinking"] = level
                return 0
            return turn
        for level in ("budget:8000", "effort:high", "off", None):
            with self.subTest(level=level):
                self.run_seat("reviewer", turn=seen(level))
                with ledger.connect(self.root / "ledger.sqlite") as con:
                    got = con.execute("SELECT thinking FROM runs ORDER BY rowid DESC").fetchone()[0]
                self.assertEqual(got, level)


for _name in [n for n in dir(sm.Worker) if n.startswith("test_")]:
    setattr(Finished, _name, None)   # its own tests run in test_seat_models


class GitHub(unittest.TestCase):
    LOOP = {"repo": REPO, "read_token": "reader", "reviewers": ["critic"]}

    def fake(self, prs, reviews, fail=()):
        def api(loop, path, method="GET", body=None, login=None):
            self.assertEqual((method, login), ("GET", "reader"))
            if path in fail:
                return None
            if "/pulls?state=all" in path:
                page = int(path.rsplit("page=", 1)[1])
                return prs[(page - 1) * 100:page * 100]
            return reviews.get(int(path.split("/pulls/")[1].split("/")[0]), [])
        return api

    def pr(self, number, author, created, merged=None, state="closed"):
        return {"number": number, "user": {"login": author}, "created_at": iso(created),
                "merged_at": iso(merged) if merged else None, "state": state}

    def review(self, login, state, at):
        return {"user": {"login": login}, "state": state, "submitted_at": iso(at)}

    def test_prs_merges_reviews_and_rounds(self):
        since = NOW - 7 * DAY
        prs = [self.pr(3, "coder", NOW - 1000, None, "open"),
               self.pr(2, "coder", NOW - 5000, NOW - 1400),
               self.pr(1, "human", NOW - 9000, NOW - 8400),
               self.pr(0, "coder", since - 10, since + 10)]           # opened before the window
        reviews = {2: [self.review("critic", "CHANGES_REQUESTED", NOW - 4880),
                       self.review("critic", "APPROVED", NOW - 2000),
                       self.review("arbiter", "APPROVED", NOW - 1500)],
                   # Another reviewer's change request is not a round of the loop's reviewer.
                   1: [self.review("critic", "APPROVED", NOW - 8940),
                       self.review("arbiter", "CHANGES_REQUESTED", NOW - 8900)],
                   3: []}
        with mock.patch.object(gh, "api", side_effect=self.fake(prs, reviews)):
            got = stats.github(self.LOOP, since)
        self.assertEqual((got["opened"], got["merged"], got["open"], got["partial"]),
                         (3, 2, 1, False))
        self.assertEqual(got["authors"], {"coder": 2, "human": 1})
        self.assertEqual(got["reviewers"]["arbiter"]["states"],
                         {"CHANGES_REQUESTED": 1, "APPROVED": 1})
        self.assertEqual(got["open_to_merge"]["all"]["median"], 2100)   # 3600 s and 600 s
        self.assertEqual(got["open_to_merge"]["coder"]["max"], 3600)
        critic = got["reviewers"]["critic"]
        self.assertEqual((critic["reviews"], critic["states"]),
                         (3, {"CHANGES_REQUESTED": 1, "APPROVED": 2}))
        self.assertEqual(critic["first_review"]["median"], 90)       # 120 s and 60 s
        self.assertEqual(got["changes_requested_rounds"], {"0": 1, "1": 1})
        text = stats.text({"repo": REPO, "loop": "w", "since": since, "until": NOW,
                           "turns": {}, "github": got})
        self.assertIn("PRs opened 3 · merged 2 · open 1", text)
        self.assertIn("change-request rounds per reviewed PR: 0: 1, 1: 1", text)

    def test_a_failed_read_is_a_lower_bound(self):
        prs = [self.pr(1, "coder", NOW - 100)]
        failing = {f"/repos/{REPO}/pulls/1/reviews?per_page=100"}
        with mock.patch.object(gh, "api", side_effect=self.fake(prs, {}, failing)):
            got = stats.github(self.LOOP, NOW - DAY)
        self.assertTrue(got["partial"])
        self.assertIn("lower bounds", "\n".join(stats._github_parts(got)[0]))
        with mock.patch.object(gh, "api", return_value=None):
            self.assertTrue(stats.github(self.LOOP, NOW - DAY)["partial"])

    def test_a_page_that_crosses_the_window_start_is_the_last_one_read(self):
        prs = [self.pr(n, "coder", NOW - n * 60) for n in range(100)]     # newest first
        since = NOW - 49.5 * 60                                           # 50 inside the window
        fake = self.fake(prs, {})

        def api(loop, path, *args, **kwargs):
            self.assertNotIn("page=2", path)
            return fake(loop, path, *args, **kwargs)
        with mock.patch.object(gh, "api", side_effect=api):
            self.assertEqual(stats.github(self.LOOP, since)["opened"], 50)

    def test_html_escapes_what_github_says(self):
        prs = [self.pr(1, "<script>x</script>", NOW - 100)]
        reviews = {1: [self.review("<script>y</script>", "APPROVED", NOW - 50)]}
        with mock.patch.object(gh, "api", side_effect=self.fake(prs, reviews)):
            got = stats.github(self.LOOP, NOW - DAY)
        page = stats.as_html({"repo": REPO, "loop": "w", "since": NOW - DAY, "until": NOW,
                              "turns": {}, "github": got})
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})

    def cli(self, *argv):
        return t.run_cli(t.parser_for({}).parse_args(list(argv)))

    def test_text_json_and_html(self):
        loop_id = json.loads((t.LOOPS_DIR / "widgets.json").read_text())["id"]
        db = run_supervisor.production_ledger()
        sup = Supervisor(db)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("stats-1", t.REPO, 41, "a" * 40, "reviewer")
        with ledger.connect(db) as con:
            con.execute("UPDATE runs SET state='succeeded', launch_intent=created+5, "
                        "finished=created+65, error=? WHERE delivery='stats-1'", (SECRET,))
        rc, out = self.cli("stats", "--loop", loop_id)
        self.assertEqual(rc, 0, out)
        self.assertIn("reviewer", out)
        self.assertIn("60 s", out)
        rc, out = self.cli("stats", "--loop", loop_id, "--json")
        self.assertEqual(json.loads(out)["turns"]["reviewer"]["states"]["succeeded"] >= 1, True)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            page = pathlib.Path(tmp) / "index.html"
            rc, out = self.cli("stats", "--loop", loop_id, "--html", str(page))
            self.assertEqual(rc, 0, out)
            self.assertTrue(page.read_text().startswith("<!doctype html>"))
            self.assertNotIn("exploded", page.read_text())
        rc, out = self.cli("stats", "--loop", loop_id, "--since", "soon")
        self.assertEqual(rc, 2)
        self.assertIn("--since", out)
        rc, out = self.cli("stats", "--loop", "nope")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
