"""The Desktop page (#369): a read-only backend over the run ledger, and a plugin.js that stays
inside Hermes's Desktop SDK surface.

Real SQLite ledgers; the FastAPI layer is exercised only when ``fastapi`` is importable, and
``node --check`` only when node is installed. Rendering in the Desktop app is not tested here.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import config, desktop_api, ledger, run_supervisor, stats  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
NOW = 1_800_000_000.0
SECRET = "/home/someone/.hermes/tokens/x.pat exploded"
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


class Now(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.n = 0

    def row(self, state, updated, error=None, seat="reviewer", repo=REPO):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", repo, 300 + self.n, "a" * 40, seat)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, updated=?, error=? WHERE delivery=?",
                        (state, updated, error, f"d{self.n}"))

    def test_live_runs_and_the_last_days_failures_without_their_text(self):
        self.row("running", NOW - 10)
        self.row("waiting", NOW - 20, "held: waiting for CI on aaaaaaa — 1 check(s) still running")
        self.row("waiting", NOW - 30, "turn exited with status 1")
        self.row("uncertain", NOW - 40, SECRET, seat="fixer")
        self.row("failed", NOW - 50, SECRET)
        self.row("failed", NOW - 2 * 86400, "old")                  # outside the day
        self.row("succeeded", NOW - 5)                              # finished: not "now"
        self.row("running", NOW - 5, repo="other/repo")
        view = stats.now(self.db, REPO, at=NOW)
        states = [run["state"] for run in view["runs"]]
        self.assertEqual(states, ["running", "waiting", "waiting", "uncertain", "failed"])
        whys = [run["why"] for run in view["runs"]]
        self.assertEqual(whys[1], "held: waiting for CI on aaaaaaa — 1 check(s) still running")
        self.assertEqual(whys[2:], ["waiting to retry", "needs an operator: may have written",
                                    "failed: see explain"])
        self.assertNotIn("exploded", json.dumps(view))
        self.assertEqual(view["counts"], {"running": 1, "held": 1, "waiting": 1, "uncertain": 1,
                                          "failed": 1})
        self.assertEqual(view["runs"][0]["head"], "aaaaaaa")

    def test_no_ledger(self):
        self.assertIsNone(stats.now(self.db.with_name("missing.sqlite"), REPO))

    def test_counts_and_stuck_cover_every_live_run_past_the_page(self):
        # The oldest runs (an uncertain one, a held one) fall off the newest-100 page.
        self.row("uncertain", NOW - 5000, SECRET, seat="fixer")
        self.row("waiting", NOW - 4000, "held: waiting for CI")
        for i in range(stats.NOW_PAGE + 20):
            self.row("running", NOW - 100 + i * 0.01)
        view = stats.now(self.db, REPO, at=NOW)
        self.assertEqual(len(view["runs"]), stats.NOW_PAGE)
        self.assertTrue(view["truncated"])
        self.assertEqual(view["total"], stats.NOW_PAGE + 22)
        self.assertEqual(view["counts"], {"running": stats.NOW_PAGE + 20, "held": 1, "uncertain": 1})
        self.assertNotIn("uncertain", [run["state"] for run in view["runs"]])
        oldest = stats.stuck(self.db, REPO, NOW)
        self.assertEqual((oldest["state"], oldest["since"]), ("uncertain", NOW - 5000))

    def test_a_short_list_is_not_truncated(self):
        self.row("running", NOW - 10)
        view = stats.now(self.db, REPO, at=NOW)
        self.assertFalse(view["truncated"])
        self.assertEqual(view["total"], 1)


class Api(unittest.TestCase):
    LOOPS = [{"id": "widgets", "repo": REPO}, {"id": "gadgets", "repo": "acme/gadgets"}]

    def loops(self, found):
        return mock.patch.object(config, "readable_loops", return_value=(found, []))

    def test_loops_lists_only_ids_and_repos(self):
        with self.loops([{**self.LOOPS[0], "tokens": {"x": "/secret"}}]):
            self.assertEqual(desktop_api.loops(),
                             [{"id": "widgets", "repo": REPO, "unreadable": False, "reason": ""}])

    # ---- #701: a loop whose config does not load is named, never dropped ----------------

    def broken(self, found, *unreadable):
        return mock.patch.object(config, "readable_loops", return_value=(found, list(unreadable)))

    def test_loops_names_an_unreadable_loop_with_a_bounded_reason(self):
        with self.broken(self.LOOPS[:1], ("typo", "bad json\n" + "x" * 5000)):
            listed = desktop_api.loops()
        self.assertEqual([entry["id"] for entry in listed], ["widgets", "typo"])
        self.assertEqual(len({frozenset(entry) for entry in listed}), 1)    # same keys
        self.assertFalse(listed[0]["unreadable"])
        typo = listed[1]
        self.assertTrue(typo["unreadable"])
        self.assertEqual(typo["repo"], "")
        self.assertLessEqual(len(typo["reason"]), 200)
        self.assertNotIn("\n", typo["reason"])

    def test_overview_needs_you_counts_uncertain_runs_beyond_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "runs.sqlite"
            sup = Supervisor(db)
            with mock.patch.object(sup, "_spawn"):
                for i in range(stats.NOW_PAGE + 5):
                    sup.enqueue(f"d{i}", REPO, 400 + i, "a" * 40, "reviewer")
            with ledger.connect(db) as con:
                con.execute("UPDATE runs SET state='running', updated=? ", (NOW,))
                con.execute("UPDATE runs SET state='uncertain', updated=? WHERE delivery='d0'",
                            (NOW - 9000,))
            with mock.patch.object(desktop_api.config, "readable_loops",
                                   return_value=(self.LOOPS[:1], [])), \
                 mock.patch.object(desktop_api.run_supervisor, "production_ledger",
                                   return_value=db):
                [widgets] = desktop_api.overview_view()["loops"]
        self.assertEqual(widgets["needs_you"], 1)

    def test_overview_names_an_unreadable_loop_beside_the_readable_one(self):
        with self.broken(self.LOOPS[:1], ("typo", "bad json")), \
             mock.patch.object(stats, "now",
                               return_value={"repo": REPO, "runs": [], "counts": {"uncertain": 1}}) as now:
            widgets, typo = desktop_api.overview_view()["loops"]
        self.assertEqual(widgets["needs_you"], 1)
        self.assertFalse(widgets["unreadable"])
        self.assertEqual(typo, {"id": "typo", "repo": "", "available": False, "error": True,
                                "unreadable": True, "reason": "bad json", "counts": {},
                                "needs_you": 0})
        now.assert_called_once()                    # the unreadable loop has no repo to read

    def test_loops_and_overview_agree_on_which_loops_exist(self):
        with self.broken(self.LOOPS, ("typo", "bad json")), \
             mock.patch.object(stats, "now", return_value=None):
            configured = [entry["id"] for entry in desktop_api.loops()]
            read = [entry["id"] for entry in desktop_api.overview_view()["loops"]]
        self.assertEqual(sorted(configured), sorted(read))
        self.assertIn("typo", configured)

    def test_a_per_loop_view_explains_an_unreadable_loop_instead_of_no_such_loop(self):
        with self.broken(self.LOOPS[:1], ("typo", "bad json")), \
             self.assertRaises(desktop_api.NotFound) as caught:
            desktop_api.now_view("typo")
        self.assertIn("unreadable", str(caught.exception))
        self.assertIn("bad json", str(caught.exception))
        # the default is not silently the one loop that loads while another is configured
        with self.broken(self.LOOPS[:1], ("typo", "bad json")), \
             self.assertRaises(desktop_api.NotFound) as caught:
            desktop_api.now_view(None)
        self.assertIn("typo (unreadable)", str(caught.exception))

    def test_a_real_unparsable_file_is_named_by_loops_and_overview(self):
        directory = config.config_dir()
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        broken = directory / "typo.json"
        broken.write_text("{ not json\n")

        def cleanup():
            broken.unlink(missing_ok=True)
            if created and directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        self.addCleanup(cleanup)
        self.assertIn("typo", [e["id"] for e in desktop_api.loops() if e["unreadable"]])
        with mock.patch.object(stats, "now", return_value=None):
            view = desktop_api.overview_view()
        self.assertIn("typo", [e["id"] for e in view["loops"] if e["unreadable"]])

    def test_a_loop_must_exist_and_one_is_the_default(self):
        with self.loops(self.LOOPS), self.assertRaises(desktop_api.NotFound):
            desktop_api.now_view(None)                              # two loops: name one
        with self.loops(self.LOOPS), self.assertRaises(desktop_api.NotFound):
            desktop_api.now_view("nope")
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "now", return_value=None) as now, \
             mock.patch.object(desktop_api.pacing, "seats", return_value={"seats": {}, "holds": []}):
            self.assertEqual(desktop_api.now_view(None),
                             {"repo": REPO, "runs": [], "counts": {}, "available": False,
                              "pacing": {"seats": {}, "holds": []}})
        self.assertEqual(now.call_args.args[1], REPO)

    def test_now_carries_ledger_availability_never_a_fabricated_zero(self):
        # #slice2 defect 2: stats.now returns None when the ledger file is absent. The view must
        # say so (available false) instead of shipping empty counts that read as a verified zero.
        pacing = mock.patch.object(desktop_api.pacing, "seats", return_value={"seats": {}, "holds": []})
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "now", return_value=None), pacing:
            absent = desktop_api.now_view("widgets")
        self.assertIs(absent["available"], False)        # omitted entirely by the reverted fix
        self.assertEqual(absent["runs"], [])
        self.assertEqual(absent["counts"], {})
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "now",
                               return_value={"repo": REPO, "runs": [], "counts": {"uncertain": 1}}), pacing:
            present = desktop_api.now_view("widgets")
        self.assertIs(present["available"], True)
        self.assertEqual(present["counts"], {"uncertain": 1})

    def test_overview_lists_every_loop_with_its_counts_and_availability(self):
        def fake_now(db, repo, at=None):
            if repo == REPO:
                return {"repo": repo, "runs": [], "counts": {"uncertain": 2, "running": 1}}
            return None                                  # gadgets has no ledger
        with self.loops(self.LOOPS), \
             mock.patch.object(stats, "now", side_effect=fake_now), \
             mock.patch.object(run_supervisor, "production_ledger",
                               return_value=pathlib.Path("/nonexistent/runs.sqlite")):
            view = desktop_api.overview_view()
        self.assertEqual([loop["id"] for loop in view["loops"]], ["widgets", "gadgets"])
        widgets, gadgets = view["loops"]
        self.assertEqual(widgets, {"id": "widgets", "repo": REPO, "available": True, "error": False,
                                   "unreadable": False, "reason": "",
                                   "counts": {"uncertain": 2, "running": 1}, "needs_you": 2})
        self.assertEqual(gadgets, {"id": "gadgets", "repo": "acme/gadgets", "available": False,
                                   "error": False, "unreadable": False, "reason": "",
                                   "counts": {}, "needs_you": 0})
        self.assertIsInstance(view["read_at"], float)

    def test_overview_keeps_the_other_loops_when_one_read_fails(self):
        def fake_now(db, repo, at=None):
            if repo == REPO:
                return {"repo": repo, "runs": [], "counts": {"uncertain": 1}}
            raise sqlite3.OperationalError("database disk image is malformed")
        with self.loops(self.LOOPS), mock.patch.object(stats, "now", side_effect=fake_now):
            widgets, gadgets = desktop_api.overview_view()["loops"]
        self.assertIs(widgets["available"], True)
        self.assertIs(widgets["error"], False)
        self.assertEqual(widgets["needs_you"], 1)
        self.assertIs(gadgets["available"], False)
        self.assertIs(gadgets["error"], True)            # the failure is reported per loop
        self.assertEqual(gadgets["counts"], {})          # never a manufactured count
        self.assertEqual(gadgets["needs_you"], 0)

    def test_overview_takes_no_loop_argument_and_reads_no_ledger_of_its_own(self):
        # It is a host view over every configured loop; it names none, so an empty config is an
        # empty list, not an error.
        with self.loops([]), \
             mock.patch.object(stats, "now") as now:
            self.assertEqual(desktop_api.overview_view()["loops"], [])
        now.assert_not_called()

    def test_stats_reads_the_ledger_only(self):
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "collect", return_value={"ok": 1}) as collect:
            self.assertEqual(desktop_api.stats_view("widgets", "30d"), {"ok": 1})
        self.assertIs(collect.call_args.kwargs["with_github"], False)
        with self.loops(self.LOOPS[:1]), self.assertRaises(desktop_api.BadRequest):
            desktop_api.stats_view("widgets", "1y")

    def test_missing_ledger_stats_matches_pinned_desktop_payload(self):
        expected = json.loads((ROOT / "tests/fixtures/desktop_no_ledger_stats.json").read_text())
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            db = pathlib.Path(temp) / "missing.sqlite"
            loop = {"id": expected["loop"], "repo": expected["repo"]}
            actual = stats.collect(loop, db, expected["since"], with_github=False,
                                   now=expected["until"])
            self.assertEqual(actual, expected)
            self.assertIsNone(actual["turns"])
            self.assertFalse(db.exists())
            with self.loops([loop]), \
                 mock.patch.object(run_supervisor, "production_ledger", return_value=db), \
                 mock.patch.object(stats.time, "time", return_value=expected["until"]):
                self.assertEqual(desktop_api.stats_view(loop["id"], "7d"), expected)


class Pr(unittest.TestCase):
    """One PR's record: the host's own next-step line, what the ledger recorded, and — the
    promise the whole page rests on — a run's error text never reaching the view."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.n = 0
        # Its own state dir: findings live in the loop's state files, not the ledger.
        self.loops = [{"id": "widgets", "repo": REPO, "cap": 3,
                       "state_dir": str(pathlib.Path(temp.name) / "state")}]

    def patch_all(self):
        return mock.patch.object(config, "readable_loops", return_value=(self.loops, [])), \
            mock.patch.object(run_supervisor, "production_ledger", return_value=self.db)

    def row(self, state, error=None, seat="reviewer", pr=626, head=None, retry_at=None):
        self.n += 1
        # The ledger's own unique key is (repo, pr, head, seat, turn_key), so rows for one PR
        # need their own heads — that is what a turn per head means.
        head = head or chr(ord("a") + self.n) * 40
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", REPO, pr, head, seat)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, error=?, updated=?, retry_at=? WHERE delivery=?",
                        (state, error, time.time(), retry_at, f"d{self.n}"))
        return f"d{self.n}"

    def test_the_next_step_is_the_host_line_and_a_runs_error_text_never_appears(self):
        self.row("waiting", "held: waiting on a review window")      # host-written reason
        self.row("waiting", SECRET)                                  # retry wait, error not shown
        self.row("uncertain", SECRET, seat="fixer")
        self.row("failed", SECRET)
        loops, prod = self.patch_all()
        with loops, prod:
            view = desktop_api.pr_view("widgets", 626)
        self.assertEqual(len(view["stuck"]), 4)
        for stuck in view["stuck"]:
            self.assertTrue(stuck["next"], stuck)
            self.assertNotIn("exploded", stuck["next"])
        self.assertNotIn("exploded", json.dumps(view))
        self.assertEqual([t["state"] for t in view["turns"]],
                         ["waiting", "waiting", "uncertain", "failed"])

    def test_verdicts_are_the_receipts_github_states_and_the_cap_is_the_loops(self):
        # One receipt per run — its primary key is run_id — so one run per verdict. The runs go
        # in first: a single write transaction cannot wait on itself.
        deliveries = [self.row("succeeded") for _ in range(3)]
        with ledger.connect(self.db) as con:
            ids = {row[1]: (row[0], row[2])
                   for row in con.execute("SELECT id, delivery, head FROM runs")}
            for verdict, delivery in zip(("APPROVED", "CHANGES_REQUESTED", "APPROVED"), deliveries):
                con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                            "review_id,verdict,created) VALUES(?,?,?,?,?,?,?)",
                            (ids[delivery][0], "confirmed", "g1", 42, self.n, verdict, time.time()))
            run_id, head = ids[deliveries[0]]
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, REPO, 626, head, "t1", "REJECT", "no", time.time(), time.time()))
        loops, prod = self.patch_all()
        with loops, prod:
            view = desktop_api.pr_view("widgets", 626)
        self.assertEqual(view["verdicts"], {"APPROVED": 2, "CHANGES_REQUESTED": 1})
        self.assertEqual(view["cap"], 3)
        self.assertEqual([r["verdict"] for r in view["rulings"]], ["REJECT"])
        self.assertEqual(view["rulings"][0]["head"], head[:7])

    def test_a_long_review_shows_the_newest_turns_and_rulings_and_the_true_totals(self):
        extra = 7
        with mock.patch.object(desktop_api, "TURNS_MAX", 5):
            for _ in range(5 + extra):
                self.row("succeeded")
            with ledger.connect(self.db) as con:
                rows = con.execute("SELECT id, head FROM runs ORDER BY id").fetchall()
                # created is the order the page shows: stamp it so the oldest row is first.
                for n, (run_id, head) in enumerate(rows):
                    con.execute("UPDATE runs SET created=? WHERE id=?", (1000.0 + n, run_id))
                    con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,"
                                "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                                (run_id, REPO, 626, head, f"t{n}", f"V{n}", "x", 1000.0 + n,
                                 1000.0 + n))
            loops, prod = self.patch_all()
            with loops, prod:
                view = desktop_api.pr_view("widgets", 626)
        last = 5 + extra - 1
        self.assertEqual([t["created"] for t in view["turns"]],
                         [1000.0 + n for n in range(last - 4, last + 1)])     # newest, oldest first
        self.assertEqual(view["turns_total"], 5 + extra)
        self.assertEqual([r["verdict"] for r in view["rulings"]],
                         [f"V{n}" for n in range(last - 4, last + 1)])
        self.assertEqual(view["rulings_total"], 5 + extra)

    def test_findings_come_from_the_loops_own_state_with_the_hosts_open_flag(self):
        from diaktoros import state as state_mod
        state_mod.state_for(self.loops[0]).findings_put(626, {"last_head": "a" * 40, "findings": {
            "F2": {"state": "fixed", "text": ": second", "head": "a" * 40, "missed": False},
            "F1": {"state": "open", "text": ": the first one", "head": "b" * 40, "missed": True},
        }})
        loops, prod = self.patch_all()
        with loops, prod:
            view = desktop_api.pr_view("widgets", 626)
        self.assertEqual([f["id"] for f in view["findings"]], ["F1", "F2"])   # numeric, not lexical
        self.assertEqual([f["open"] for f in view["findings"]], [True, False])
        self.assertEqual(view["findings"][0]["text"], "the first one")        # the id separator gone
        self.assertTrue(view["findings"][0]["missed"])
        self.assertEqual(view["findings"][0]["head"], "bbbbbbb")

    def test_a_pr_the_reviewer_never_numbered_has_no_findings(self):
        loops, prod = self.patch_all()
        with loops, prod:
            self.assertEqual(desktop_api.pr_view("widgets", 626)["findings"], [])

    def test_gate_decisions_come_from_the_hosts_own_log(self):
        rows = [{"gate": "gate_reviewer", "event": "pull_request", "action": "opened",
                 "decision": "accepted", "reason": "reviewer turn enqueued", "number": 626,
                 "sender": "tuck-coe", "time": "2026-10-12T23:08:55Z", "head": "aaaaaaa"}]
        loops, prod = self.patch_all()
        with loops, prod, mock.patch.object(desktop_api.gate_decisions, "for_pr",
                                            return_value=rows) as for_pr:
            view = desktop_api.pr_view("widgets", 626)
        self.assertEqual(view["decisions"], rows)
        self.assertEqual(for_pr.call_args.args, (self.loops[0], 626))

    def test_an_unreadable_decision_log_is_empty_never_an_error(self):
        loops, prod = self.patch_all()
        with loops, prod, mock.patch.object(desktop_api.gate_decisions, "for_pr",
                                            side_effect=OSError("gone")):
            self.assertEqual(desktop_api.pr_view("widgets", 626)["decisions"], [])

    def test_a_bad_pr_number_and_an_unknown_loop_are_refused(self):
        loops, prod = self.patch_all()
        with loops, prod:
            for bad in (0, -1, "626", 3.5, True):
                with self.subTest(bad), self.assertRaises(desktop_api.BadRequest):
                    desktop_api.pr_view("widgets", bad)
            with self.assertRaises(desktop_api.NotFound):
                desktop_api.pr_view("nope", 626)

    def test_no_ledger_is_an_empty_record_not_an_error(self):
        loops, prod = self.patch_all()
        with loops, mock.patch.object(run_supervisor, "production_ledger",
                                      return_value=self.db.with_name("missing.sqlite")):
            view = desktop_api.pr_view("widgets", 626)
        self.assertEqual(view["turns"], [])
        self.assertEqual(view["stuck"], [])
        self.assertEqual(view["verdicts"], {})

    def test_the_pr_view_creates_no_files_of_its_own(self):
        # The reader policy this view uses: a ledger with no live WAL is opened immutable, which
        # creates neither -wal nor -shm. The stats views (mode=ro) may leave a -shm behind —
        # SQLite's own reader mechanics — so this asserts the new view, not the whole page.
        self.row("waiting", "held: a reason")
        before = sorted(p.name for p in self.db.parent.iterdir())
        loops, prod = self.patch_all()
        with loops, prod:
            desktop_api.pr_view("widgets", 626)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before)

    def test_both_views_leave_the_ledgers_records_untouched(self):
        self.row("waiting", "held: a reason")

        def snapshot():
            with ledger.connect(self.db) as con:
                return (con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                        con.execute("SELECT MAX(updated) FROM runs").fetchone()[0],
                        con.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0])

        before = snapshot()
        loops, prod = self.patch_all()
        with loops, prod:
            desktop_api.pr_view("widgets", 626)
            desktop_api.now_view("widgets")
        self.assertEqual(snapshot(), before)


class Health(unittest.TestCase):
    def loops(self, found):
        return mock.patch.object(config, "readable_loops", return_value=(found, []))

    def test_the_checks_are_the_doctors_own_and_offline(self):
        checks = [desktop_api.doctor.Check("config", "verified", "ok", ""),
                  desktop_api.doctor.Check("token:x", "mismatch", "missing", "create it"),
                  desktop_api.doctor.Check("gateway", "unknown", "not probed (--offline)", "")]
        with self.loops([{"id": "widgets", "repo": REPO}]), \
             mock.patch.object(desktop_api.doctor, "check_loop", return_value=checks) as called:
            view = desktop_api.health_view("widgets")
        self.assertIs(called.call_args.kwargs["offline"], True)
        self.assertEqual(view["counts"], {"verified": 1, "mismatch": 1, "unknown": 1})
        self.assertEqual(view["checks"][1], {"name": "token:x", "status": "mismatch",
                                             "detail": "missing", "fix": "create it",
                                             "paused": False})

    def test_an_unknown_loop_is_refused(self):
        with self.loops([{"id": "widgets", "repo": REPO}]), \
             self.assertRaises(desktop_api.NotFound):
            desktop_api.health_view("nope")

    def test_the_probe_runs_the_checks_online(self):
        with self.loops([{"id": "widgets", "repo": REPO}]), \
             mock.patch.object(desktop_api.doctor, "check_loop", return_value=[]) as called:
            view = desktop_api.health_view("widgets", True)
        self.assertIs(called.call_args.kwargs["offline"], False)
        self.assertIs(view["offline"], False)

    def test_the_probe_is_refused_off_this_machine_before_it_reads_anything(self):
        # Presence is the signal, not the value: an empty header came from a proxy just as much
        # as a filled one, and this is the case the first version of the guard let through.
        proxied = ({"x-forwarded-for": "203.0.113.9"}, {"x-forwarded-for": ""},
                   {"forwarded": "for=203.0.113.9"}, {"x-real-ip": "203.0.113.9"},
                   {"x-forwarded-host": "hermes.example"}, {"x-forwarded-proto": "https"},
                   {"X-Forwarded-Port": "443"})
        cases = [("10.0.0.5", None), (None, {"x-forwarded-for": "203.0.113.9"})]
        cases += [("127.0.0.1", headers) for headers in proxied]      # loopback, but proxied
        with self.loops([{"id": "widgets", "repo": REPO}]), \
             mock.patch.object(desktop_api.doctor, "check_loop") as called:
            for host, headers in cases:
                with self.subTest(host=host, headers=headers), \
                     self.assertRaises(desktop_api.Forbidden):
                    desktop_api.health_view("widgets", True, host, headers)
        called.assert_not_called()                  # refused before a token is opened
        # The offline view reads no credential, so it answers whoever asks.
        with self.loops([{"id": "widgets", "repo": REPO}]), \
             mock.patch.object(desktop_api.doctor, "check_loop", return_value=[]):
            self.assertTrue(desktop_api.health_view("widgets", False, "10.0.0.5")["offline"])

    def test_probe_is_local_allows_a_local_peer_and_nothing_proxied(self):
        for host in ("127.0.0.1", "::1", "localhost", None, ""):
            for headers in (None, {}):              # no forwarding header at all
                self.assertTrue(desktop_api.probe_is_local(host, headers), (host, headers))
        self.assertFalse(desktop_api.probe_is_local("10.0.0.5", None))
        for headers in ({"x-forwarded-for": "203.0.113.9"}, {"x-forwarded-for": ""}):
            self.assertFalse(desktop_api.probe_is_local("127.0.0.1", headers), headers)

    def test_probe_is_local_refuses_any_x_forwarded_header(self):
        # A proxy that sets only X-Forwarded-Proto (or any other member of the family) is a proxy.
        for headers in ({"x-forwarded-proto": "https"}, {"X-Forwarded-Proto": ""},
                        {"x-forwarded-port": "443"}, {"x-forwarded-prefix": "/a"},
                        {"host": "localhost", "x-forwarded-proto": "https"}):
            self.assertFalse(desktop_api.probe_is_local("127.0.0.1", headers), headers)
        # Unrelated headers do not refuse.
        self.assertTrue(desktop_api.probe_is_local("127.0.0.1", {"host": "localhost", "x-forwarding": "1"}))


DASHBOARD_ROUTES = {"/loops", "/stats", "/now", "/overview", "/pr", "/health", "/settings"}
# The page stopped being read-only for one confirmed action (#371): POST /retry. Every other route
# is a read, and this set is enumerated so a new route cannot ship unlisted.
DASHBOARD_WRITE_ROUTES = {"/retry"}


class RouteListing(unittest.TestCase):
    """Reads dashboard/plugin_api.py as text, so it runs whether or not fastapi is installed."""

    def source(self):
        return (ROOT / "dashboard" / "plugin_api.py").read_text()

    def decorators(self):
        return re.findall(r"^@router\.(\w+)\(\s*(?:\"([^\"]*)\")?", self.source(), re.M)

    def test_every_route_is_a_listed_get_except_the_one_confirmed_write(self):
        found = self.decorators()
        self.assertTrue(found)
        writes = {path for verb, path in found if verb != "get"}
        self.assertEqual(writes, DASHBOARD_WRITE_ROUTES)
        self.assertEqual({verb for verb, _ in found}, {"get", "post"})
        for verb, path in found:
            if verb == "post":
                self.assertIn(path, DASHBOARD_WRITE_ROUTES)
        paths = [path for _, path in found]
        self.assertEqual(sorted(paths), sorted(set(paths)))
        self.assertEqual(set(paths), DASHBOARD_ROUTES | DASHBOARD_WRITE_ROUTES)

    def test_no_route_is_registered_another_way(self):
        text = self.source()
        self.assertNotRegex(text, r"(?m)^\s*(?:\w+\.)?(?:add_api_route|include_router|api_route)\(")
        self.assertEqual(len(re.findall(r"@router\.", text)), len(self.decorators()))


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "fastapi is not installed here")
class Routes(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("hrl_plugin_api", ROOT / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)

    def request(self, host="127.0.0.1", headers=None):
        return types.SimpleNamespace(client=types.SimpleNamespace(host=host),
                                     headers=dict(headers or {}))

    def test_reads_are_get_and_the_one_write_is_a_mapped_post(self):
        from fastapi import HTTPException
        verbs = {(m, route.path) for route in self.api.router.routes for m in route.methods}
        self.assertEqual({method for method, _ in verbs}, {"GET", "POST"})
        self.assertEqual({path for method, path in verbs if method == "POST"},
                         DASHBOARD_WRITE_ROUTES)
        paths = {route.path for route in self.api.router.routes}
        self.assertEqual(paths, DASHBOARD_ROUTES | DASHBOARD_WRITE_ROUTES)
        with mock.patch.object(desktop_api, "now_view", side_effect=desktop_api.NotFound("no")), \
             self.assertRaises(HTTPException) as caught:
            self.api.now("x")
        self.assertEqual(caught.exception.status_code, 404)
        with mock.patch.object(desktop_api, "pr_view",
                               side_effect=desktop_api.BadRequest("pr must be positive")), \
             self.assertRaises(HTTPException) as caught:
            self.api.pr(0)
        self.assertEqual(caught.exception.status_code, 400)
        with mock.patch.object(desktop_api, "health_view",
                               side_effect=desktop_api.Forbidden("local only")), \
             self.assertRaises(HTTPException) as caught:
            self.api.health(self.request(), "widgets", True)
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_retry_route_passes_the_confirmation_and_maps_every_refusal(self):
        from fastapi import HTTPException
        payload = {"loop": "widgets", "pr": 356, "seat": "reviewer", "head": "a" * 40}
        with mock.patch.object(desktop_api, "retry_view", return_value={"ok": True}) as retry_view:
            self.assertEqual(self.api.retry(dict(payload)), {"ok": True})
        self.assertEqual(retry_view.call_args.args, ("widgets", 356, "reviewer", "a" * 40))
        # A retry the ledger refuses is a 409 carrying the host's own reason — never a 500, and
        # never a 2xx with a hidden failure.
        with mock.patch.object(desktop_api, "retry_view",
                               side_effect=desktop_api.Conflict("refused: run is uncertain")), \
             self.assertRaises(HTTPException) as caught:
            self.api.retry(dict(payload))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "refused: run is uncertain")
        with mock.patch.object(desktop_api, "retry_view",
                               side_effect=desktop_api.BadRequest("seat must be one of reviewer")), \
             self.assertRaises(HTTPException) as caught:
            self.api.retry({**payload, "seat": "nope"})
        self.assertEqual(caught.exception.status_code, 400)

    def test_the_overview_route_takes_no_argument(self):
        with mock.patch.object(desktop_api, "overview_view", return_value={"loops": []}) as overview_view:
            self.assertEqual(self.api.overview(), {"loops": []})
        overview_view.assert_called_once_with()

    def test_the_settings_route_takes_no_argument(self):
        with mock.patch.object(desktop_api, "settings_view", return_value={"plugin": {}}) as settings_view:
            self.assertEqual(self.api.settings(), {"plugin": {}})
        settings_view.assert_called_once_with()

    def test_the_pr_route_passes_the_number_and_health_its_scope_and_caller(self):
        with mock.patch.object(desktop_api, "pr_view", return_value={"ok": 1}) as pr_view:
            self.assertEqual(self.api.pr(626, "widgets"), {"ok": 1})
        self.assertEqual(pr_view.call_args.args, ("widgets", 626))
        with mock.patch.object(desktop_api, "health_view", return_value={"ok": 1}) as health_view:
            self.assertEqual(self.api.health(self.request(), "widgets", True), {"ok": 1})
        self.assertEqual(health_view.call_args.args, ("widgets", True, "127.0.0.1", {}))
        with mock.patch.object(desktop_api, "health_view", return_value={"ok": 1}) as health_view:
            self.api.health(self.request("10.0.0.5", {"x-forwarded-for": "203.0.113.9"}))
        self.assertEqual(health_view.call_args.args,
                         (None, False, "10.0.0.5", {"x-forwarded-for": "203.0.113.9"}))
        # The whole header mapping goes down: an empty value is still a forwarding header.
        with mock.patch.object(desktop_api, "health_view", return_value={"ok": 1}) as health_view:
            self.api.health(self.request("127.0.0.1", {"x-forwarded-for": ""}))
        self.assertEqual(health_view.call_args.args, (None, False, "127.0.0.1", {"x-forwarded-for": ""}))


class Package(unittest.TestCase):
    def test_the_manifest_mounts_the_backend_without_a_dashboard_tab(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        plugin_name = re.search(r"^name:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
        self.assertEqual(manifest["name"], plugin_name)
        self.assertIs(manifest["tab"]["hidden"], True)
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue((ROOT / "dashboard" / manifest["api"]).is_file())
        self.assertTrue((ROOT / "dashboard" / manifest["entry"]).is_file())

    def test_plugin_js_names_the_package_and_ships_off(self):
        source = PLUGIN_JS.read_text()
        plugin_name = re.search(r"^name:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
        self.assertIn(f"const ID = '{plugin_name}'", source)
        self.assertIn("defaultEnabled: false", source)

    def test_slice2_plugin_has_a_per_loop_baseline_and_a_loop_scoped_drawer(self):
        source = PLUGIN_JS.read_text()
        self.assertIn("const NEEDS_YOU_PREFIX = 'needsYou:'", source)   # keyed by loop id
        self.assertNotIn("NEEDS_YOU_KEY", source)                        # the single global key is gone
        self.assertIn("'drawer-' + openPr.loop", source)                 # drawer identity carries the loop
        self.assertIn("readView('/overview', 'overview')", source)
        # The chip->page handoff is a cache write the page observes, not a route string the app
        # would have to re-render on.
        self.assertIn("queryClient.setQueryData(FOCUS_KEY", source)
        self.assertIn("useQuery({ queryKey: FOCUS_KEY", source)
        self.assertNotIn("window.addEventListener", source)              # stays inside the SDK surface

    def test_first_render_findings_641(self):
        source = PLUGIN_JS.read_text()
        code = re.sub(r"(?<![:\w])//[^\n]*", "", source)
        # no markdown backticks in rendered strings (they print literally)
        self.assertNotIn("`", code)
        # seat rows show the states that have no column of their own
        self.assertIn("otherStates(s)", source)
        self.assertIn("'other'", source)
        # a clean install gets one line and a collapsed list, not five arbitrary rows
        self.assertNotIn("checks.slice(0, 5)", source)
        self.assertIn("cleanLine(counts, checks.length)", source)


# Hermes's catalog "desktop surface" rules (hermes_cli/plugin_validate_desktop.py), plus the
# loader's import allowlist and the SDK's theming rule (theme variables, never a literal color).
FORBIDDEN = (
    re.compile(r"\b[A-Za-z_$][\w$]*\.prototype\.[\w$]+\s*=[^=]"),
    re.compile(r"\bObject\.definePropert(?:y|ies)\(\s*[\w$.]+\.prototype\b"),
    re.compile(r"\b(?:Reflect|Object)\.setPrototypeOf\(|\.__proto__\s*="),
    re.compile(r"(?<![\w$.])eval\(|\bnew\s+Function\("),
    re.compile(r"\bimport\("),
    re.compile(r"createElement\(\s*['\"]script['\"]\s*\)|<script\b"),
    re.compile(r"\bdocument\."),
    re.compile(r"\binnerHTML\b|\bdangerouslySetInnerHTML\b"),
    re.compile(r"\bwindow\.(?:setInterval|setTimeout|addEventListener)\b"),
    re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\("),
)
ALLOWED_IMPORTS = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}


class Surface(unittest.TestCase):
    def test_stays_inside_the_sdk_surface(self):
        code = re.sub(r"(?<![:\w])//[^\n]*", "", PLUGIN_JS.read_text())
        for pattern in FORBIDDEN:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(code))
        imports = set(re.findall(r"^import\b[^'\"]*['\"]([^'\"]+)['\"]", code, re.M))
        self.assertEqual(imports - ALLOWED_IMPORTS, set())

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_components_with_sdk_query_fixtures(self):
        done = subprocess.run(
            ["node", "--experimental-vm-modules", "--test", "--test-reporter=tap",
             str(ROOT / "tests" / "desktop_behavior.mjs")],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # A green exit code is not evidence the suite ran: a binary that prints a version and
        # stops, or a runner that skips everything, also exits 0 (a test-harness fixture once
        # replaced the operator's Node with a stub). Require reported tests, no failures, and a
        # named test.
        ok = re.findall(r"^ok \d+ - ", done.stdout, re.M)
        self.assertGreaterEqual(len(ok), 77, done.stdout[-4000:])
        self.assertNotIn("not ok", done.stdout)
        reported_pass = re.search(r"^# pass (\d+)$", done.stdout, re.M)
        if reported_pass is None:
            self.fail(done.stdout[-4000:])
        self.assertEqual(int(reported_pass.group(1)), len(ok))
        self.assertRegex(done.stdout, r"(?m)^# skipped 0$")
        self.assertRegex(done.stdout, r"(?m)^ok \d+ - settings: production projection renders every schema field and dynamically added unknown type$")
        # This slice's own named proof: the real registered page, clicked through confirm → post.
        self.assertRegex(done.stdout, r"(?m)^ok \d+ - page: confirming posts the exact target, then shows the answer and the state read back$")
        self.assertRegex(done.stdout, r"(?m)^ok \d+ - page: a refusal shows the backend reason, keeps the dialog open and invalidates nothing$")
        # The drawer's truncation wording (#821): the named proof that a cut list says so.
        self.assertRegex(done.stdout, r"(?m)^ok \d+ - page: the drawer says when its turns and rulings are the newest of more, and only then$")
        self.assertIn("palette only: unseen successful cache read expires", done.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_it_parses_as_an_es_module(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            copy = pathlib.Path(tmp) / "plugin.mjs"
            copy.write_text(PLUGIN_JS.read_text())
            done = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
