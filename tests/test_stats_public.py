"""``hermes dk stats --public --json`` (#2253): what the loop's checks caught, safe to publish.

A fixture ledger seeded with a title, a body, turn output, tokens, a cost, a login and a local
path in every place the ledger holds them, so the export is checked against real leaks, not
an empty table.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diaktoros import stats_public
from diaktoros.run_supervisor import ledger_schema

REPO = "o/r"
T = 1_790_000_000.0                       # 2026-09-21, UTC
SECRET = "ghp_" + "A" * 36
LEAKS = ("TITLE-LEAK", "BODY-LEAK", "OUTPUT-LEAK", "/home/jeremy/.hermes", "drey-coe1",
         SECRET, "tokens_in", "tokens_out", "cost", "login", "title", "body", "4242")


def ledger(path: Path) -> Path:
    """A ledger with: PR 7 approved at head a, the shadow asked for changes, the arbiter
    upheld it (stopped); PR 8 the same but overruled; PR 9 a cap ruling with no shadow."""
    con = sqlite3.connect(path)
    con.executescript(ledger_schema.SCHEMA)
    ledger_schema.migrate(con)

    def run(rid, pr, head, seat, at, model="terra"):
        con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, created, "
                    "updated, error, detail, model, tokens_in, tokens_out, cost) VALUES "
                    "(?,?,?,?,?,?,'succeeded',?,?,?,?,?,4242,4242,42.42)",
                    (rid, rid, REPO, pr, head, seat, at, at, "OUTPUT-LEAK /home/jeremy/.hermes",
                     "OUTPUT-LEAK " + SECRET, model))

    def review(rid, pr, head, verdict, at, model="terra"):
        run(rid, pr, head, "reviewer", at, model)
        con.execute("INSERT INTO review_receipts VALUES (?, 'posted', 'g', 1, 9, ?, ?, NULL)",
                    (rid, verdict, at))

    def shadow(rid, pr, head, verdict, at):
        run(rid, pr, head, "shadow", at, "glm")
        con.execute("INSERT INTO shadow_reviews VALUES (?,?,?,?,'glm',?,?,1,NULL,NULL,?)",
                    (rid, REPO, pr, head, verdict, "BODY-LEAK drey-coe1", at))

    def ruling(rid, pr, head, verdict, shadow_run, at):
        run(rid, pr, head, "adjudicator", at)
        con.execute("INSERT INTO rulings (run_id, repo, pr, head, turn_key, verdict, body, "
                    "created, updated, shadow_run) VALUES (?,?,?,?,'k',?,?,?,?,?)",
                    (rid, REPO, pr, head, verdict, "BODY-LEAK", at, at, shadow_run))

    for pr, verdict in ((7, "REJECT"), (8, "ACCEPT")):
        review(f"r{pr}", pr, "a", "APPROVE", T + pr)
        shadow(f"s{pr}", pr, "a", "REQUEST_CHANGES", T + pr + 1)
        ruling(f"j{pr}", pr, "a", verdict, f"s{pr}", T + pr + 2)
    review("r9a", 9, "b", "REQUEST_CHANGES", T + 20, model="glm")
    review("r9b", 9, "c", "REQUEST_CHANGES", T + 21, model="glm")
    ruling("j9", 9, "c", "REJECT", "", T + 22)                  # the cap's: no shadow
    con.execute("INSERT INTO filed_issues VALUES ('r7', 1, ?, 7, 'a', 'TITLE-LEAK', 'k1', 1, "
                "'posted', 100, NULL, ?, ?)", (REPO, T, T))
    con.execute("INSERT INTO shadow_issues (run_id, seq, repo, pr, head, title, title_key, body, "
                "labels, state, issue_number, created, updated) VALUES ('s7', 1, ?, 7, 'a', "
                "'TITLE-LEAK', 'k2', 'BODY-LEAK', '[]', 'posted', 101, ?, ?)", (REPO, T, T))
    con.execute("INSERT INTO change_proofs VALUES (?, 7, 'a', '{\"x\": \"OUTPUT-LEAK\"}', ?)",
                (REPO, T + 3))
    con.commit()
    con.close()
    return path


def listing(loop, since):
    """GitHub's closed issues and PRs: 100 completed, 101 not planned, PRs 7 and 9 merged."""
    return {100: {"reason": "completed", "created": T, "merged": None},
            101: {"reason": "not_planned", "created": T, "merged": None},
            7: {"reason": "", "created": T, "merged": T + 3600},
            9: {"reason": "", "created": T, "merged": T + 7200}}, False


class Export(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.db = ledger(tmp / "ledger.db")
        self.loop = {"id": "l", "repo": REPO, "read_token": "drey-coe1"}

    def export(self, **kw) -> dict:
        return stats_public.collect(self.loop, self.db, 0, T + 1000, listing=listing, **kw)

    def test_a_reject_over_a_shadow_at_an_approved_head_is_stopped_an_accept_overruled(self):
        data = self.export()
        self.assertEqual(data["stopped"], {"count": 1, "prs": [7]})
        self.assertEqual(data["overruled"], {"count": 1})     # PR 8; PR 9's cap ruling is neither

    def test_a_cap_ruling_is_neither_stopped_nor_overruled(self):
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM rulings WHERE pr IN (7, 8)")
        con.commit()
        con.close()
        data = self.export()
        self.assertEqual((data["stopped"]["count"], data["overruled"]["count"]), (0, 0))

    def test_findings_cycle_and_window(self):
        data = self.export()
        self.assertEqual(data["findings"], {"reviewer": {"filed": 1, "completed": 1},
                                            "shadow": {"filed": 1, "completed": 0},
                                            "arbiter": {"filed": 0, "completed": 0}})
        self.assertEqual(data["cycle"]["rounds"], {"n": 2, "median": 1.5, "p75": 1.75})
        self.assertEqual(data["cycle"]["open_to_merge"]["median"], 5400)
        self.assertEqual(data["cycle"]["change_proofs"], 1)
        self.assertEqual(data["window"], {"first": "2026-09-21", "last": "2026-09-21",
                                          "prs": 3, "turns": 9})
        self.assertEqual(data["schema"], 1)

    def test_no_title_body_output_token_cost_login_or_path_leaves_the_host(self):
        text = json.dumps(self.export(with_models=True))
        for leak in LEAKS:
            self.assertNotIn(leak, text)

    def test_models_only_when_asked(self):
        self.assertNotIn("models", self.export())
        rows = {row["model"]: row for row in self.export(with_models=True)["models"]}
        self.assertEqual((rows["terra"]["verdicts"], rows["terra"]["approve_share"]), (2, 1.0))
        self.assertEqual((rows["terra"]["shadowed_approvals"], rows["terra"]["shadow_disagreed"],
                          rows["terra"]["upheld"], rows["terra"]["overruled"]), (2, 2, 1, 1))
        self.assertEqual(rows["glm"]["approve_share"], 0.0)
        self.assertEqual(rows["glm"]["first"], "2026-09-21")

    def test_no_ledger_yet_is_an_empty_export(self):
        data = stats_public.collect(self.loop, self.db.parent / "none.db", 0, T, listing=listing)
        self.assertEqual((data["stopped"]["count"], data["window"]["turns"]), (0, 0))
        self.assertFalse((self.db.parent / "none.db").exists())

    def test_a_failed_listing_says_unknown_rather_than_zero(self):
        data = stats_public.collect(self.loop, self.db, 0, T + 1000,
                                    listing=lambda loop, since: (None, True))
        self.assertTrue(data["partial"])
        self.assertIsNone(data["findings"]["reviewer"]["completed"])
        self.assertIsNone(data["cycle"]["rounds"])


class Command(Export):
    def run_cli(self, *argv) -> tuple[int, str]:
        from diaktoros.cli import reports
        root = argparse.ArgumentParser()
        reports.add_stats(root.add_subparsers())
        args = root.parse_args(["stats", *argv])
        out = io.StringIO()
        with (mock.patch("diaktoros.config.load_id", return_value=self.loop),
              mock.patch("diaktoros.run_supervisor.production_ledger", return_value=self.db),
              mock.patch.object(stats_public, "closed_listing", listing),
              contextlib.redirect_stdout(out)):
            return args.func(args), out.getvalue()

    def test_public_json_prints_the_whole_ledger_by_default(self):
        code, out = self.run_cli("--loop", "l", "--public", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["stopped"]["prs"], [7])
        self.assertNotIn("models", data)
        self.assertIn("models", json.loads(self.run_cli("--loop", "l", "--public", "--json",
                                                        "--models")[1]))

    def test_public_refuses_without_json_and_models_without_public(self):
        for argv in (("--public",), ("--json", "--models"), ("--public", "--json", "--github")):
            code, out = self.run_cli("--loop", "l", *argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("--public needs --json", out)


if __name__ == "__main__":
    unittest.main()
