"""``hermes dk history --pr N`` (#1657): one PR's review cycle as a timeline, read-only.

The ledger, the state dir and the parser are real; GitHub's four reads are the only fakes. The
fixture is a merged PR with two rounds, a fixer push and answers, a shadow review, a ruling and
a filed issue.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import cli, config, gh, history, history_render, ledger, run_supervisor  # noqa: E402

A, B = "a" * 40, "b" * 40
T = 1_790_000_000.0                                 # 2026-09-21 14:13:20 UTC


def iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))



TOKEN = "ghp_" + "Z" * 30
PR = {"number": 7, "state": "closed", "merged_at": iso(T + 600),
      "merged_by": {"login": "coe0718"}}
REVIEWS = [{"id": 101, "commit_id": A, "state": "CHANGES_REQUESTED", "user": {"login": "vex"},
            "submitted_at": iso(T + 30), "html_url": "https://example.test/r101",
            "body": "F1 [correctness] a.py:3 P1 the loop never ends"},
           {"id": 102, "commit_id": B, "state": "CHANGES_REQUESTED", "user": {"login": "vex"},
            "submitted_at": iso(T + 330), "html_url": "https://example.test/r102",
            "body": "F1: fixed\nF2 [requirement] b.py:1 no docs"}]
COMMITS = [{"sha": B, "commit": {"message": "Stop the loop\n\nbody"}, "author": {"login": "drey"},
            "html_url": "https://example.test/c"}]
COMMENTS = [{"user": {"login": "coe0718"}, "created_at": iso(T + 500),
             "body": "looks fine to me", "html_url": "https://example.test/k"}]


class History(fg.Base):
    def setUp(self):
        super().setUp()
        self.db = run_supervisor.production_ledger()
        self.sup = run_supervisor.Supervisor(self.db)
        self.reads = []

    def run_row(self, seat, head, at, key="", state="succeeded", **cols):
        delivery = f"{seat}:{head[:1]}:{key}:{at}"
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.submit(delivery, fg.REPO, 7, head, seat, turn_key=key)
        cols = {"state": state, "created": at, "launch_intent": at, "finished": at + 60, **cols}
        with ledger.connect(self.db) as con:
            con.execute(f"UPDATE runs SET {', '.join(f'{k}=?' for k in cols)} WHERE delivery=?",
                        (*cols.values(), delivery))
            return con.execute("SELECT id FROM runs WHERE delivery=?", (delivery,)).fetchone()[0]

    def insert(self, sql, *values):
        with ledger.connect(self.db) as con:
            con.execute(sql, values)

    def cycle(self):
        """The fixture: round 1 at A (review, shadow, fix and push), round 2 at B (review,
        ruling, a filed issue), merged by a person."""
        r1 = self.run_row("reviewer", A, T, model="gpt-x", tokens_in=400_000, tokens_out=10_000)
        self.insert("INSERT INTO review_receipts(run_id,state,generation,principal_id,review_id,"
                    "verdict,created,confirmed) VALUES(?,?,?,?,?,?,?,?)",
                    r1, "confirmed", "{}", 1, 101, "REQUEST_CHANGES", T + 30, T + 30)
        shadow = self.run_row("shadow", A, T + 5)
        self.insert("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,authorized,"
                    "created) VALUES(?,?,?,?,?,?,?,?,?)",
                    shadow, fg.REPO, 7, A, "deepseek-flash", "REQUEST_CHANGES", "F1 agreed", 1, T + 40)
        fix = self.run_row("fixer", A, T + 100)
        self.insert("INSERT INTO fixer_answers(run_id,repo,pr,base,head,body,state,comment_id,"
                    "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    fix, fg.REPO, 7, A, B, "F1 fixed — loop bounded", "posted", 55, T + 150, T + 150)
        audit = config.state_dir(self.loop) / "broker-audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text(json.dumps({"repo": fg.REPO, "pr": 7, "old_head": A, "new_head": B,
                                     "role": "fixer", "login": "drey", "operation": "push",
                                     "phase": "reconciled", "outcome": "published", "at": T + 140})
                         + "\n{torn\n")
        r2 = self.run_row("reviewer", B, T + 300)
        self.insert("INSERT INTO review_receipts(run_id,state,generation,principal_id,review_id,"
                    "verdict,created,confirmed) VALUES(?,?,?,?,?,?,?,?)",
                    r2, "confirmed", "{}", 1, 102, "REQUEST_CHANGES", T + 330, T + 330)
        self.insert("INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,state,"
                    "issue_number,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    r2, 0, fg.REPO, 7, B, "Follow-up: docs", "k", 1, "posted", 1651, T + 335, T + 335)
        adj = self.run_row("adjudicator", B, T + 400, key="breach:3")
        self.insert("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                    "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                    adj, fg.REPO, 7, B, "breach:3", "ACCEPT", "the worst case is litter",
                    T + 450, T + 450)
        self.st.findings_put(7, {"findings": {
            "F1": {"state": "fixed", "head": A, "category": "correctness", "at": T + 30,
                   "text": "[correctness] a.py:3 P1 the loop never ends"},
            "F2": {"state": "open", "head": B, "category": "requirement", "at": T + 330,
                   "text": "[requirement] b.py:1 no docs"}}})

    def github(self):
        def read(key, value):
            def call(*_args, **_kwargs):
                self.reads.append(key)
                return value, ""
            return call
        return mock.patch.multiple(gh, fetch=read("pr", PR), reviews_read=read("reviews", REVIEWS),
                                   pr_commits_read=read("commits", COMMITS),
                                   issue_comments_read=read("comments", COMMENTS))

    def collect(self, offline=False):
        with self.github():
            return history.collect(self.loop, self.db, 7, offline, lambda text: text)

    def test_a_merged_pr_s_rounds_seats_ruling_and_finding_lives(self):
        self.cycle()
        out = history_render.text(self.collect())
        for want in ("round 1 @ aaaaaaa", "round 2 @ bbbbbbb",
                     "reviewer (vex) REQUEST_CHANGES (review 101) — F1 [correctness] a.py:3",
                     "turn succeeded (gpt-x · 410k tok)",
                     "shadow REQUEST_CHANGES (deepseek-flash)",
                     "fixer (drey) pushed bbbbbbb: Stop the loop",
                     "answers (posted): F1 fixed — loop bounded",
                     "ruling ACCEPT (breach:3) — \"the worst case is litter\"",
                     "filed #1651: Follow-up: docs", "coe0718 comment: looks fine to me",
                     "merged 09-21 14:23 by coe0718",
                     "F1 [P1 correctness] raised at round 1, fixed at round 2",
                     "F2 [requirement] raised at round 2, overruled by a ruling ACCEPT (breach:3) "
                     "at round 2"):
            self.assertIn(want, out)
        self.assertLess(out.index("round 1 @"), out.index("pushed bbbbbbb"))
        self.assertLess(out.index("pushed bbbbbbb"), out.index("round 2 @"))
        self.assertNotIn("gap", out)                       # every turn wrote; the torn line skipped

    def test_markdown_and_json_carry_the_same_content(self):
        self.cycle()
        data = self.collect()
        md = history_render.markdown(data)
        again = json.loads(json.dumps(data))               # what --format json prints parses back
        self.assertEqual(again["findings"], data["findings"])
        for rnd in data["rounds"]:
            self.assertIn(f"## Round {rnd['round']} @ [`{rnd['head'][:7]}`]", md)
            for event in rnd["events"]:
                self.assertIn(event["text"], md)
        self.assertIn("| F2 | — | requirement | round 2 | overruled by a ruling ACCEPT", md)
        self.assertIn("(https://example.test/r101)", md)
        self.assertIn(f"https://github.com/{fg.REPO}/pull/7#issuecomment-55", md)
        self.assertIn(f"https://github.com/{fg.REPO}/issues/1651", md)

    def test_offline_reads_no_github_and_says_what_is_missing(self):
        self.cycle()
        data = self.collect(offline=True)
        self.assertEqual(self.reads, [])
        out = history_render.text(data)
        self.assertIn("not read (--offline): the PR's comments", out)
        self.assertIn("merge state not read (--offline)", out)
        self.assertIn("F1 [P1 correctness] raised at round 1, fixed (round not on record)", out)
        self.assertNotIn("comment:", out)

    def test_a_missing_or_unreadable_store_and_a_writeless_turn_are_gaps(self):
        with self.github():
            data = history.collect(self.loop, self.root / "absent.sqlite", 7, False, str)
        self.assertTrue(any("no run ledger" in gap for gap in data["gaps"]))
        self.run_row("reviewer", A, T)                     # ended, but nothing it wrote is on record
        config.state_dir(self.loop).mkdir(parents=True, exist_ok=True)
        (config.state_dir(self.loop) / "findings.json").write_text("{not json")
        out = history_render.text(self.collect())
        self.assertIn("findings.json is unreadable", out)
        self.assertIn("[gap: ended without a write on record]", out)

    def test_a_turn_still_coming_or_uncertain_is_no_writeless_gap(self):
        """#2133: pending, claimed, blocked and uncertain turns have not ended without a write."""
        for at, state in enumerate(("pending", "claimed", "blocked", "waiting")):
            self.run_row("reviewer", A, T + at, key=f"k{at}", state=state)
        self.run_row("fixer", A, T + 10, state="uncertain")
        out = history_render.text(self.collect())
        self.assertNotIn("gap", out)
        self.assertIn("fixer turn uncertain (model unknown) — its write's outcome is unknown", out)

    def test_a_push_the_journal_could_not_confirm_says_so(self):
        """#2135: the journal's own outcome words; published reads as a plain push."""
        audit = config.state_dir(self.loop) / "broker-audit.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"repo": fg.REPO, "pr": 7, "old_head": A, "new_head": new, "role": "fixer",
                  "operation": "push", "outcome": outcome, "at": T + n}
                 for n, (new, outcome) in enumerate(((B, "published_pr_unverified"),
                                                     ("c" * 40, "unchanged"), ("d" * 40, "unknown")))]
        audit.write_text("".join(json.dumps(line) + "\n" for line in lines))
        out = history_render.text(self.collect(offline=True))
        self.assertIn("fixer pushed bbbbbbb\n", out)
        self.assertIn("pushed ccccccc (not confirmed: the branch did not move)", out)
        self.assertIn("pushed ddddddd (outcome unknown)", out)

    def test_a_token_in_a_turn_s_output_tail_is_redacted(self):
        self.run_row("fixer", A, T, state="failed",
                      detail=f"push said {TOKEN} at /home/agent/loops/x/state.json")
        for form in ("text", "markdown", "json"):
            rc, out = self.run_cli("--format", form)
            self.assertEqual(rc, 0)
            self.assertNotIn(TOKEN, out)
            self.assertIn("[REDACTED]", out)
            self.assertNotIn("/home/agent", out)             # #2134: no local path, only its end
            self.assertIn("…/state.json", out)

    def test_the_parser_drives_it_and_json_is_the_data(self):
        self.cycle()
        rc, out = self.run_cli("--format", "json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual((data["schema"], data["pr"], data["merged"]["by"]), (1, 7, "coe0718"))
        self.assertEqual([item["outcome"] for item in data["findings"]], ["fixed", "overruled"])

    def run_cli(self, *argv):
        ctx = type("Ctx", (), {"register_cli_command": lambda s, n, h, setup, **k:
                               setattr(s, "setup", setup)})()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes dk")
        ctx.setup(parser)
        args = parser.parse_args(["history", "--pr", "7", *argv])
        out = io.StringIO()
        with self.github(), redirect_stdout(out):
            rc = args.func(args)
        return rc, out.getvalue()


if __name__ == "__main__":
    unittest.main()
