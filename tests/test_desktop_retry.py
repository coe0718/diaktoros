"""Issue #371: the Desktop page's checked, confirmed Retry, on real isolated ledgers.

Every case here is the production path: a real SQLite ledger written by the real ``Supervisor``,
a real loop config file read by the production loader, and ``desktop_api.retry_view`` — the
function behind ``POST /retry`` — over ``retry_action`` and ``Supervisor.retry``. Nothing calls
GitHub, opens a credential, or launches a worker: the loop has no private runtime file, which is
the same state an unarmed loop is in.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import builtins
import io
import json
import os
import pathlib
import re
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from diaktoros import config, desktop_api, ledger, retry_action, run_supervisor
from diaktoros.run_supervisor import FIXER_NOT_ADMITTED, Supervisor

REPO = "acme/widgets"
HEAD = "a" * 40
OTHER = "b" * 40
SECRET = "upstream HTTP 401 Bearer ghp_realtokenvalue /home/someone/.pat exploded"


class Fixture(unittest.TestCase):
    """An isolated home, a real loop config file, and a real ledger."""

    LOOP = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
            "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
            "seats": {"reviewer": {"profile": "r", "route": "review"},
                      "fixer": {"profile": "f", "route": "fix"}},
            "tokens": {"reviewer": "TOKEN_PATH", "dev": "TOKEN_PATH"},
            "read_token": "reader", "host": "http://127.0.0.1:9"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        self.token_path = self.home / "tokens" / "reviewer.pat"
        self.token_path.parent.mkdir()
        self.token_path.write_text("ghp_realtokenvalue\n")
        loops = self.home / "review-loops.d"
        loops.mkdir()
        self.loop = {**self.LOOP, "seats": dict(self.LOOP["seats"]),
                     "state_dir": str(self.home / "state"),
                     "tokens": {login: str(self.token_path) for login in ("reviewer", "dev")}}
        (loops / "widgets.json").write_text(json.dumps(self.loop))
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                           "DIAKTOROS_CONFIG_DIR": str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.db = run_supervisor.production_ledger()
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        self.sup._spawn = lambda: None
        self.n = 0

    # -- ledger rows ---------------------------------------------------------------------------

    def row(self, state="failed", seat="reviewer", pr=356, head=HEAD, error=None, **columns):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", REPO, pr, head, seat)
        sets = {"state": state, "error": error, "updated": time.time()}
        sets.update(columns)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET " + ", ".join(f"{key}=?" for key in sets) +
                        " WHERE delivery=?", (*sets.values(), f"d{self.n}"))
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM runs WHERE delivery=?",
                                    (f"d{self.n}",)).fetchone())

    def receipt(self, run_id, state="claimed"):
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,created) "
                        "VALUES(?,?,?,?,?)", (run_id, state, "g", 1, 0))

    def ruling(self, run_id, seat="adjudicator"):
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, REPO, 356, HEAD, "breach:3", "ACCEPT", "ok", 0, 0))

    def state_of(self, run_id):
        with ledger.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    # -- views --------------------------------------------------------------------------------

    def now(self):
        return desktop_api.now_view("widgets")

    def retry(self, **kwargs):
        kwargs.setdefault("head", HEAD)
        kwargs.setdefault("seat", "reviewer")
        kwargs.setdefault("pr", 356)
        return desktop_api.retry_view("widgets", kwargs["pr"], kwargs["seat"], kwargs["head"])

    def rows(self):
        return {row["seat"]: row for row in self.now()["runs"]}


class NowCarriesTheBackendVerdict(Fixture):
    """The button's input is the host's plan for that row — never a state the page matches on."""

    def test_a_failed_pre_write_run_is_retryable_with_the_full_head(self):
        row = self.row("failed", error="retry limit (4 attempts): turn exited with status 3")
        [run] = self.now()["runs"]
        self.assertEqual(run["retry"], {"eligible": True, "head": HEAD, "reason": ""})
        self.assertEqual(run["head"], HEAD[:7])
        self.assertEqual(run["state"], "failed")
        self.assertNotIn("exploded", json.dumps(self.now()))   # a run's error text never reaches it
        self.assertTrue(row["id"])

    def test_an_uncertain_run_is_listed_but_never_offered(self):
        self.row("uncertain", error="worker lost after launch intent")
        [run] = self.now()["runs"]
        self.assertFalse(run["retry"]["eligible"])
        self.assertIn("may have written", run["retry"]["reason"])

    def test_a_run_with_a_receipt_claim_is_never_offered(self):
        row = self.row("failed", error="turn exited with status 1")
        self.receipt(row["id"], "claimed")
        [run] = self.now()["runs"]
        self.assertFalse(run["retry"]["eligible"])
        self.assertIn("receipt", run["retry"]["reason"])

    def test_a_running_row_is_not_offered(self):
        self.row("running", error=None)
        [run] = self.now()["runs"]
        self.assertFalse(run["retry"]["eligible"])
        self.assertIn("running", run["retry"]["reason"])

    def test_a_failed_row_at_an_abandoned_head_names_the_head_retry_targets(self):
        # #126: the retry targets the newest head that still has an offerable row, so an older
        # failed row gets no button — and the reason says which head does.
        self.row("failed", head=HEAD, updated=time.time() - 10)
        self.row("failed", head=OTHER, updated=time.time())
        runs = self.rows()
        old = next(run for run in self.now()["runs"] if run["head"] == HEAD[:7])
        self.assertFalse(old["retry"]["eligible"])
        self.assertIn("retry targets " + OTHER[:7], old["retry"]["reason"])
        new = [run for run in self.now()["runs"] if run["head"] == OTHER[:7]]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0]["retry"]["eligible"], runs)

    def test_a_push_policy_cancelled_fixer_run_is_listed_and_offered_once_pushes_are_on(self):
        cancellable = FIXER_NOT_ADMITTED
        self.row("cancelled", seat="fixer", error=cancellable)
        [run] = [run for run in self.now()["runs"] if run["seat"] == "fixer"]
        self.assertEqual(run["state"], "cancelled")         # listed: it is the operator's to recover
        self.assertFalse(run["retry"]["eligible"])
        self.assertIn("unattended fixer pushes are off", run["retry"]["reason"])
        self.assertIn("hermes dk fixer-push --loop widgets --enable", run["retry"]["reason"])
        # With the opt-in in force now, the same row is offered.
        config_path = pathlib.Path(os.environ["DIAKTOROS_CONFIG_DIR"]) / "widgets.json"
        config_path.write_text(json.dumps({**self.loop, "unattended_fixer_push": True}))
        [run] = [run for run in self.now()["runs"] if run["seat"] == "fixer"]
        self.assertTrue(run["retry"]["eligible"], run["retry"])

    def test_a_superseded_cancellation_is_listed_nowhere_and_offered_nowhere(self):
        self.row("cancelled", error="PR head moved before the review started")
        self.assertEqual(self.now()["runs"], [])

    def test_an_unreadable_ledger_offers_no_action(self):
        view = desktop_api.now_view("widgets")
        self.assertEqual(view["runs"], [])
        with mock.patch.object(run_supervisor, "read_only_connect", return_value=None):
            self.assertEqual(desktop_api._with_retry(self.db, REPO, [], self.loop), [])

    def test_a_failed_retry_read_is_not_reported_as_a_missing_ledger(self):
        # Two different unavailable answers: a ledger that is not there, and a read that failed.
        rows = [{"pr": 356, "seat": "reviewer", "head": HEAD[:7], "state": "failed", "why": "x"}]
        with mock.patch.object(retry_action, "page_targets", side_effect=RuntimeError("boom")):
            [run] = desktop_api._with_retry(self.db, REPO, rows, self.loop)
        self.assertFalse(run["retry"]["eligible"])
        self.assertIn("could not be read", run["retry"]["reason"])
        self.assertNotIn("no run ledger", run["retry"]["reason"])
        with mock.patch.object(run_supervisor, "read_only_connect", return_value=None):
            [run] = desktop_api._with_retry(self.db, REPO, rows, self.loop)
        self.assertIn("no run ledger", run["retry"]["reason"])


class RetryTheEligible(Fixture):
    """The one write: the same operation the CLI performs, answered with the host's words."""

    def test_it_re_arms_a_failed_pre_write_run_and_reads_the_state_back(self):
        row = self.row("failed", error="retry limit (4 attempts): turn exited with status 3",
                       retries=4, budget=60)
        view = self.retry()
        self.assertGreater(len(view["rearmed"]), 0)
        after = self.state_of(row["id"])
        self.assertEqual(after["state"], "pending")
        self.assertEqual(after["retries"], 0)                       # the automatic budget is reset
        self.assertEqual(after["retry_at"], None)
        self.assertEqual(after["budget"], float(config.turn_budget(self.loop, "reviewer")))
        # The answer is the host's own line, and the read-back is the ledger after the write.
        self.assertEqual(view["head"], HEAD)
        self.assertEqual(view["head7"], HEAD[:7])
        self.assertIn("reviewer #356 @ aaaaaaa re-armed (was failed)", view["answer"])
        self.assertEqual(view["runs"], [{"seat": "reviewer", "head7": HEAD[:7], "state": "pending"}])
        self.assertEqual(view["rearmed"], [{"seat": "reviewer", "head7": HEAD[:7], "state": "failed"}])
        # A worker is asked to start exactly as `retry` asks, and no runtime means none did.
        self.assertIn("no private runtime file", view["worker"])
        # The operator's step is recorded for the report, once.
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT action FROM operator_actions").fetchall(),
                             [("retry",)])
        self.assertNotIn("exploded", json.dumps(view))
        self.assertLessEqual(len(view["answer"]), retry_action.REASON_MAX)

    def test_a_second_click_answers_nothing_to_retry_and_re_arms_nothing_again(self):
        row = self.row("failed")
        self.retry()
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry()
        self.assertIn("nothing to retry", str(caught.exception))
        self.assertEqual(self.state_of(row["id"])["state"], "pending")

    def test_the_head_is_only_a_confirmation_and_a_moved_head_is_refused(self):
        # The preview named HEAD; by the time it is confirmed the PR's newest run is at OTHER.
        original = self.row("failed", head=HEAD, updated=time.time() - 10)
        moved = self.row("failed", head=OTHER, updated=time.time())
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry(head=HEAD)
        message = str(caught.exception)
        self.assertIn("head moved since you confirmed " + HEAD[:7], message)
        self.assertIn("it is " + OTHER[:7] + " now", message)
        self.assertIn("nothing was re-armed", message)
        self.assertEqual(self.state_of(original["id"])["state"], "failed")
        self.assertEqual(self.state_of(moved["id"])["state"], "failed")
        # Confirming the head it actually targets re-arms that head's run.
        view = self.retry(head=OTHER)
        self.assertEqual(view["head7"], OTHER[:7])
        self.assertEqual(self.state_of(moved["id"])["state"], "pending")

    def test_a_seven_character_confirmation_is_accepted(self):
        self.row("failed")
        self.assertEqual(self.retry(head=HEAD[:7])["head"], HEAD)

    def test_the_client_never_decides_eligibility(self):
        # A client that says "this one is retryable" about a written run is still refused.
        row = self.row("failed")
        self.receipt(row["id"], "confirmed")
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry()
        self.assertIn("receipt", str(caught.exception))
        self.assertEqual(self.state_of(row["id"])["state"], "failed")


class RefusalsWithTheHostsReason(Fixture):
    """Every class the CLI refuses is refused here — 409 and the same words."""

    def refused(self, pattern, **kwargs):
        with self.assertRaises(desktop_api.Conflict) as caught:
            self.retry(**kwargs)
        message = str(caught.exception)
        self.assertRegex(message, pattern)
        self.assertLessEqual(len(message.splitlines()), 1)
        self.assertNotIn("\n", message)
        return message

    def test_uncertain_is_refused_with_reconcile_instructions(self):
        row = self.row("uncertain", error="worker lost after launch intent")
        message = self.refused("run is uncertain .*may have written")
        self.assertIn("never replayed", message)
        self.assertIn("run_supervisor reconcile DB", message)
        self.assertEqual(self.state_of(row["id"])["state"], "uncertain")

    def test_a_written_run_is_refused(self):
        row = self.row("failed")
        self.receipt(row["id"], "claimed")
        self.refused("review receipt")
        self.assertEqual(self.state_of(row["id"])["state"], "failed")

    def test_a_push_intent_is_refused(self):
        row = self.row("failed", seat="fixer", push_confirmed=1)
        self.refused("fixer push recorded", seat="fixer")
        self.assertEqual(self.state_of(row["id"])["state"], "failed")

    def test_a_ruling_is_refused(self):
        row = self.row("failed", seat="adjudicator")
        self.ruling(row["id"])
        self.refused("ruling recorded", seat="adjudicator")
        self.assertEqual(self.state_of(row["id"])["state"], "failed")

    def test_a_running_or_finished_run_is_offered_nothing(self):
        for offset, state in enumerate(("running", "claimed", "succeeded")):
            row = self.row(state, pr=356 + offset)
            # The CLI offers no run in this state either, and says which state it is in.
            self.refused("nothing to retry — reviewer " + state, pr=356 + offset)
            self.assertEqual(self.state_of(row["id"])["state"], state)

    def test_a_superseded_cancellation_is_refused(self):
        self.row("cancelled", error="PR head moved before the review started")
        self.refused("nothing to retry")

    def test_an_unknown_pr_and_a_seat_with_no_run_say_so(self):
        self.row("failed", pr=356)
        self.refused("no isolated run on record", pr=999)
        self.refused("no isolated run on record for the triage seat", seat="triage")

    def test_a_shadow_run_is_never_offered_by_the_page(self):
        self.row("failed", seat="shadow", error="shadow model failed")
        self.refused("no isolated run on record")

    def test_a_missing_ledger_refuses_without_naming_its_path(self):
        # The CLI may print the file it wanted; the page gets the fact, never a filesystem path.
        missing = self.home / "no-such-ledger.sqlite3"
        with mock.patch.object(run_supervisor, "production_ledger", return_value=missing):
            message = self.refused("no run ledger")
        self.assertNotIn(str(missing), message)
        self.assertNotIn(str(self.home), message)
        self.assertNotIn("/", message)

    def test_a_closed_pr_is_refused_from_the_watchdog_listing(self):
        self.row("failed")
        from diaktoros import state as state_mod
        st = state_mod.state_for(self.loop)
        st.watch_save({**st.watch(), "open_prs": {"at": time.time(), "numbers": [1, 2]}})
        self.refused("PR #356 is not open")
        self.assertEqual(self.rows()["reviewer"]["state"], "failed")

    def test_a_pr_the_listing_still_names_is_retried_without_a_github_read(self):
        row = self.row("failed")
        from diaktoros import state as state_mod
        st = state_mod.state_for(self.loop)
        st.watch_save({**st.watch(), "open_prs": {"at": time.time() - 86400, "numbers": [356]}})
        self.retry()                                   # stale listing, but the PR is named
        self.assertEqual(self.state_of(row["id"])["state"], "pending")

    def test_nothing_is_offered_when_the_seat_is_not_on_record(self):
        self.row("failed", seat="reviewer")
        self.refused("no isolated run on record for the fixer seat", seat="fixer")


class ScopeLock(Fixture):
    """#482: retry is the release command — but only for a retry that actually happens."""

    def hold(self):
        config_path = pathlib.Path(os.environ["DIAKTOROS_CONFIG_DIR"]) / "widgets.json"
        config_path.write_text(json.dumps({**self.loop, "fixer_scope_lock": True,
                                           "unattended_fixer_push": True}))
        from diaktoros import state as state_mod
        st = state_mod.state_for(self.loop)
        st.scope_hold_set(356, {"head": HEAD, "files": ["src/lib.rs"], "released": False})
        return st

    def test_a_successful_retry_releases_the_hold(self):
        self.row("failed", seat="fixer")
        st = self.hold()
        view = self.retry(seat="fixer")
        self.assertTrue(st.scope_holds()[356]["released"])
        self.assertTrue(view["rearmed"])

    def test_a_refused_click_never_releases_a_hold(self):
        # Every refusal decided before the re-arm — an unknown PR, a stale confirmation — leaves
        # the hold exactly where it was.
        self.row("failed", seat="fixer", head=HEAD, updated=time.time() - 10)
        self.row("failed", seat="fixer", head=OTHER, updated=time.time())
        st = self.hold()
        with self.assertRaises(desktop_api.Conflict):
            self.retry(seat="fixer", pr=999)
        self.assertFalse(st.scope_holds()[356]["released"])
        with self.assertRaises(desktop_api.Conflict):
            self.retry(seat="fixer", head=HEAD)          # the newest head is OTHER now
        self.assertFalse(st.scope_holds()[356]["released"])
        self.assertEqual(self.rows()["fixer"]["state"], "failed")


class MalformedSelection(Fixture):
    def test_bad_numbers_seats_and_heads_are_bad_requests(self):
        self.row("failed")
        for pr in (0, -1, "356", 3.5, True, None):
            with self.subTest(pr=pr), self.assertRaises(desktop_api.BadRequest):
                desktop_api.retry_view("widgets", pr, "reviewer", HEAD)
        with self.assertRaises(desktop_api.BadRequest):
            desktop_api.retry_view("widgets", 356, "nope", HEAD)
        with self.assertRaises(desktop_api.BadRequest):
            desktop_api.retry_view("widgets", 356, "reviewer", "zzzzzzz")
        with self.assertRaises(desktop_api.BadRequest):
            desktop_api.retry_view("widgets", 356, "reviewer", "abc")
        with self.assertRaises(desktop_api.BadRequest):
            desktop_api.retry_view("widgets", 356, "reviewer", "")
        with self.assertRaises(desktop_api.BadRequest):        # a confirmation is required
            desktop_api.retry_view("widgets", 356, "reviewer", None)
        self.assertEqual(self.rows()["reviewer"]["state"], "failed")

    def test_an_unknown_loop_is_a_not_found(self):
        self.row("failed")
        with self.assertRaises(desktop_api.NotFound):
            desktop_api.retry_view("nope", 356, "reviewer", HEAD)


class NoGithubAndNoCredentials(Fixture):
    """The route must not reach GitHub or open a token file — on any path."""

    def watch(self):
        opened = []
        real = builtins.open

        def recording(file, *args, **kwargs):
            opened.append(str(file))
            return real(file, *args, **kwargs)
        return opened, mock.patch.object(builtins, "open", recording)

    def test_success_refusal_and_preview_read_no_credential_and_call_no_github(self):
        from diaktoros import gh
        row = self.row("failed")
        written = self.row("failed", head=OTHER, seat="fixer")
        self.receipt(written["id"], "claimed")
        opened, recorder = self.watch()
        boom = mock.Mock(side_effect=AssertionError("the Desktop must not call GitHub"))
        with recorder, mock.patch.object(gh, "pr", boom), mock.patch.object(gh, "api", boom):
            self.now()
            self.retry()
            with self.assertRaises(desktop_api.Conflict):
                self.retry(seat="fixer", head=OTHER)
            with self.assertRaises(desktop_api.Conflict):
                self.retry(pr=999)
        boom.assert_not_called()
        self.assertFalse([path for path in opened if str(self.token_path) in path], opened)
        self.assertFalse([path for path in opened if "reviewer.pat" in path], opened)
        self.assertEqual(self.state_of(row["id"])["state"], "pending")


class OneCodePath(unittest.TestCase):
    """The page cannot drift from the CLI: it delegates, and it delegates to the same functions."""

    def test_the_view_delegates_to_the_shared_action(self):
        loop = {"id": "widgets", "repo": REPO}
        with mock.patch.object(desktop_api, "_loop", return_value=loop), \
             mock.patch.object(retry_action, "perform",
                               return_value={"loop": "widgets", "pr": 356, "seat": "reviewer",
                                             "head": HEAD, "head7": HEAD[:7], "rearmed": [
                                   {"seat": "reviewer", "head": HEAD, "head7": HEAD[:7],
                                    "state": "failed", "turn_key": ""}],
                                   "refused": [], "worker": "", "message": "", "released": None}
                               ) as perform, \
             mock.patch.object(desktop_api, "_runs_after", return_value=[]), \
             mock.patch.object(run_supervisor, "production_ledger",
                               return_value=pathlib.Path("/nonexistent/runs.sqlite")):
            view = desktop_api.retry_view("widgets", 356, "reviewer", HEAD)
        self.assertEqual(perform.call_args.args[:3], (loop, 356, "reviewer"))
        self.assertEqual(perform.call_args.kwargs["confirm_head"], HEAD)
        self.assertIsNone(perform.call_args.kwargs.get("resolve_head"))
        self.assertIsNone(perform.call_args.kwargs.get("read_pr"))
        self.assertIn("re-armed", view["answer"])

    def test_the_shared_action_uses_the_supervisors_own_retry_and_its_own_refusal(self):
        # No second implementation of the operation: the row id and the loop's current seat budget
        # go to Supervisor.retry, and a ValueError from it is what the page reports.
        loop = {"id": "widgets", "repo": REPO, "seats": {"reviewer": {"turn_budget_s": 111}}}
        rows = [{"id": "run-1", "seat": "reviewer", "head": HEAD, "turn_key": "",
                 "state": "failed", "error": "x", "updated": 1.0}]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            ledger_path = pathlib.Path(temp) / "runs.sqlite"
            ledger_path.write_text("")
            with mock.patch.object(retry_action, "plan",
                                   return_value={"head": HEAD, "head7": HEAD[:7], "rows": rows,
                                                 "offered": rows, "rearmable": rows,
                                                 "blocked": {}, "disputed": False,
                                                 "message": ""}), \
                 mock.patch.object(retry_action, "open_listing", return_value=None), \
                 mock.patch.object(retry_action.scope_lock, "release", return_value=None), \
                 mock.patch.object(Supervisor, "retry", return_value="pending") as retried, \
                 mock.patch.object(Supervisor, "record_operator_action") as recorded, \
                 mock.patch.object(retry_action.gate, "resume_isolated", return_value=False):
                result = retry_action.perform(loop, 356, "reviewer", ledger=ledger_path,
                                              confirm_head=HEAD)
        retried.assert_called_once_with("run-1", budget=111)
        recorded.assert_called_once_with(REPO, 356, "retry")
        self.assertEqual(result["rearmed"][0]["state"], "failed")
        self.assertIn("re-armed", retry_action.answer(result))

    def test_a_supervisor_refusal_is_the_answer(self):
        loop = {"id": "widgets", "repo": REPO, "seats": {}}
        rows = [{"id": "run-1", "seat": "reviewer", "head": HEAD, "turn_key": "",
                 "state": "uncertain", "error": "x", "updated": 1.0}]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            ledger_path = pathlib.Path(temp) / "runs.sqlite"
            ledger_path.write_text("")
            with mock.patch.object(retry_action, "plan",
                                   return_value={"head": HEAD, "head7": HEAD[:7], "rows": rows,
                                                 "offered": rows, "rearmable": [], "blocked": {},
                                                 "disputed": False, "message": ""}), \
                 mock.patch.object(retry_action, "open_listing", return_value=None), \
                 mock.patch.object(retry_action.scope_lock, "release", return_value=None), \
                 mock.patch.object(Supervisor, "retry",
                                   side_effect=ValueError("refused: run is uncertain (a worker "
                                                          "may have written)")):
                result = retry_action.perform(loop, 356, "reviewer", ledger=ledger_path,
                                              confirm_head=HEAD)
        self.assertFalse(result["ok"])
        self.assertEqual(result["rearmed"], [])
        self.assertIn("run is uncertain", retry_action.answer(result))


@unittest.skipUnless(__import__("importlib").util.find_spec("fastapi"),
                     "fastapi is not installed here")
class OverHttp(Fixture):
    """The route as the gateway mounts it: a real FastAPI app, real body, real status codes.

    ``TestClient`` calls the route function the mount installs — the same one
    ``/api/plugins/diaktoros/retry`` serves — so a mismatch between the body the page sends and
    the body the route reads cannot pass unnoticed.
    """

    def client(self):
        import importlib.util as util
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = util.spec_from_file_location("hrl_retry_plugin_api", root / "dashboard" / "plugin_api.py")
        api = util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix="/api/plugins/diaktoros")
        return TestClient(app)

    def test_a_confirmed_retry_answers_200_with_the_hosts_answer(self):
        self.row("failed")
        response = self.client().post("/api/plugins/diaktoros/retry",
                                      json={"loop": "widgets", "pr": 356, "seat": "reviewer",
                                            "head": HEAD})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("reviewer #356 @ aaaaaaa re-armed", body["answer"])
        self.assertEqual(body["runs"][0]["state"], "pending")
        self.assertEqual([r["state"] for r in body["rearmed"]], ["failed"])

    def test_every_refusal_is_the_status_the_page_expects(self):
        row = self.row("uncertain", error="worker lost")
        client = self.client()
        refused = client.post("/api/plugins/diaktoros/retry",
                              json={"loop": "widgets", "pr": 356, "seat": "reviewer", "head": HEAD})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("run is uncertain", refused.json()["detail"])
        self.assertEqual(self.state_of(row["id"])["state"], "uncertain")
        bad_seat = client.post("/api/plugins/diaktoros/retry",
                               json={"loop": "widgets", "pr": 356, "seat": "nope", "head": HEAD})
        self.assertEqual(bad_seat.status_code, 400)
        unknown = client.post("/api/plugins/diaktoros/retry",
                              json={"loop": "typo", "pr": 356, "seat": "reviewer", "head": HEAD})
        self.assertEqual(unknown.status_code, 404)
        missing = client.post("/api/plugins/diaktoros/retry",
                              json={"loop": "widgets", "pr": 356, "seat": "reviewer"})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("head is required", missing.json()["detail"])
        # A body that is not an object is refused before any rule runs.
        self.assertEqual(client.post("/api/plugins/diaktoros/retry", json=[1, 2]).status_code, 422)

    def test_the_route_answers_only_through_the_gateway_style_prefix(self):
        # Nothing is mounted at the root: the mount is the namespace, so a page cannot reach the
        # action except through the gateway's own /api/plugins/ path.
        self.row("failed")
        client = self.client()
        self.assertEqual(client.post("/retry", json={"loop": "widgets", "pr": 356,
                                                     "seat": "reviewer", "head": HEAD}).status_code,
                         404)


class Disclosure(unittest.TestCase):
    """The page stopped being read-only for one action; the repo says so where it ships."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_the_manifest_description_names_the_retry(self):
        manifest = json.loads((self.ROOT / "dashboard" / "manifest.json").read_text())
        self.assertIn("retry", manifest["description"].lower())
        self.assertNotRegex(manifest["description"].lower(), r"^read-only")

    def test_the_catalog_description_names_the_retry(self):
        text = (self.ROOT / "catalog" / "diaktoros.yaml").read_text()
        entry = next(line for line in text.splitlines() if line.startswith("description:"))
        self.assertIn("re-arm", entry)

    def test_the_security_page_describes_the_one_write_and_its_boundary(self):
        text = (self.ROOT / "docs" / "security.md").read_text()
        section = text[text.index("The optional Desktop page's backend routes"):]
        section = section[:section.index("\n\n\n") if "\n\n\n" in section else len(section)]
        self.assertIn("retry", section.lower())
        self.assertIn("409", section)
        self.assertIn("never calls GitHub", section)
        self.assertIn("confirmation", section.lower())

    def test_the_plugin_descriptor_discloses_the_new_action(self):
        text = (self.ROOT / "plugin.yaml").read_text()
        self.assertIn("re-arms a failed run", text)
        self.assertIn("never a GitHub read", text)

    def test_the_operations_page_lists_the_retry_route(self):
        text = (self.ROOT / "docs" / "operations.md").read_text()
        section = text[text.index("The backend (`dashboard/plugin_api.py`"):]
        self.assertIn("POST /retry", section)


if __name__ == "__main__":
    unittest.main()
