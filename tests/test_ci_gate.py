"""A reviewer cannot approve a head whose CI failed, and is told the CI state.

A reviewer approved a head whose tests had already failed (34 seconds before): it was never
shown CI, and nothing stopped the approval. ``ci.read`` reads the head's check runs and commit
statuses; the reviewer and fixer prompts carry them as host facts, and the broker refuses an
APPROVE while a check has failed or CI cannot be read — before the one write is spent, so the
seat's REQUEST_CHANGES in the same turn goes through. Offline: GitHub REST is mocked.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, ci_tests, gh  # noqa: E402
import test_partial_view_no_approve as pv  # noqa: E402

HEAD = pv.HEAD
LOOP = {"repo": pv.REPO, "read_token": "read"}


def run(name, status="completed", conclusion="success", id=1):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion}


class Fake:
    """Check runs and statuses for HEAD; ``None`` makes that read fail."""

    def __init__(self, runs=(), statuses=(), total=None, notes=None, status_pages=None):
        self.runs, self.statuses, self.total = runs, statuses, total
        self.status_pages = status_pages
        self.notes = notes or {}   # annotations by run id; a missing id is unreadable
        self.paths = []

    def __call__(self, loop, path, method="GET", body=None, login=None):
        self.paths.append((path, login))
        if "/annotations" in path:
            rid = int(path.split("/check-runs/")[1].split("/")[0])
            page = int(path.split("&page=")[1]) if "&page=" in path else 1
            if (rid, page) in self.notes:   # per-page annotations
                return self.notes[(rid, page)]
            return self.notes.get(rid) if page == 1 else None
        if "/check-runs" in path:
            if self.runs is None:
                return None
            return {"total_count": len(self.runs) if self.total is None else self.total,
                    "check_runs": list(self.runs)}
        if path.split("?")[0].endswith("/status"):
            if self.status_pages is not None:   # per-page replies; a missing page is unreadable
                return self.status_pages.get(int(path.split("&page=")[1]))
            return None if self.statuses is None else {"state": "x", "statuses": list(self.statuses)}
        raise AssertionError(path)


def read(**kwargs):
    fake = Fake(**kwargs)
    with mock.patch.object(gh, "api", side_effect=fake):
        return ci.read(LOOP, HEAD), fake


class Read(unittest.TestCase):
    def test_sorts_runs_and_statuses_and_reads_as_the_reader(self):
        state, fake = read(runs=[run("lint"), run("tests", conclusion="failure"),
                                 run("build", status="in_progress", conclusion=None),
                                 run("docs", conclusion="skipped")],
                           statuses=[{"context": "ci/legacy", "state": "error"},
                                     {"context": "deploy", "state": "pending"}])
        self.assertEqual(state.failed, ["tests", "ci/legacy"])
        self.assertEqual(state.pending, ["build", "deploy"])
        self.assertEqual(state.passed, ["docs", "lint"])
        self.assertFalse(state.green)
        self.assertTrue(all(login == "read" for _, login in fake.paths))
        self.assertIn(f"/repos/{pv.REPO}/commits/{HEAD}/check-runs?per_page=100&page=1",
                      [p for p, _ in fake.paths])

    def test_status_pages_are_all_read_and_a_failure_beyond_page_one_counts(self):
        ok = lambda n, k: [{"context": f"c{i}", "state": "success"} for i in range(n, n + k)]  # noqa: E731
        pages = {1: {"state": "failure", "total_count": 101, "statuses": ok(0, 100)},
                 2: {"state": "failure", "total_count": 101,
                     "statuses": [{"context": "late", "state": "failure"}]}}
        state, fake = read(status_pages=pages)
        self.assertEqual(state.failed, ["late"])
        self.assertFalse(state.green)
        self.assertTrue(any("/status?per_page=100&page=2" in p for p, _ in fake.paths))

    def test_a_partial_status_view_is_unreadable_not_green(self):
        ok = [{"context": f"c{i}", "state": "success"} for i in range(30)]
        # total_count says 31, only 30 in hand
        state, _ = read(status_pages={1: {"state": "failure", "total_count": 31, "statuses": ok}})
        self.assertIsNone(state)
        # an unreadable subsequent page
        full = [{"context": f"c{i}", "state": "success"} for i in range(100)]
        state, _ = read(status_pages={1: {"state": "success", "total_count": 101,
                                          "statuses": full}})
        self.assertIsNone(state)
        # combined failure with no failing context in hand
        state, _ = read(status_pages={1: {"state": "error", "total_count": 30, "statuses": ok}})
        self.assertIsNone(state)
        self.assertIn("could not read CI", ci.approval_refusal(state))

    def test_a_rerun_replaces_the_failure_it_reran(self):
        state, _ = read(runs=[run("tests", conclusion="failure", id=1), run("tests", id=2)])
        self.assertEqual((state.failed, state.passed), ([], ["tests"]))
        state, _ = read(runs=[run("tests", id=1), run("tests", conclusion="cancelled", id=2)])
        self.assertEqual((state.failed, state.cancelled), ([], ["tests"]))

    def test_cancelled_and_stale_are_their_own_state(self):
        """#363: GitHub cancelled them (no runner acquired); a re-run, not a fix."""
        state, _ = read(runs=[run("a", conclusion="cancelled"), run("b", conclusion="stale"),
                              run("c", conclusion="timed_out"), run("d")])
        self.assertEqual((state.cancelled, state.failed, state.passed), (["a", "b"], ["c"], ["d"]))
        self.assertFalse(state.green)
        self.assertFalse(ci.CIState(cancelled=["a"]).green)

    def test_a_runner_shutdown_failure_is_cancelled(self):
        """#399: GitHub reports a runner shutdown as `failure`; the annotations tell."""
        shut = [{"message": "The operation was canceled."},
                {"message": "Process completed with exit code 143."}]
        state, fake = read(runs=[run("a", conclusion="failure", id=1),
                                 run("b", conclusion="failure", id=2),
                                 run("c", conclusion="failure", id=3), run("d")],
                           notes={1: shut, 2: [{"message": "AssertionError: 1 != 2"}]})
        self.assertEqual((state.cancelled, state.failed, state.passed),
                         (["a"], ["b", "c"], ["d"]))   # real failure and unreadable stay failed
        self.assertTrue(any("/check-runs/1/annotations" in p for p, _ in fake.paths))
        self.assertFalse(state.green)
        state, _ = read(runs=[run("a", conclusion="failure", id=1)],
                        notes={1: [{"message": "The runner has received a shutdown signal"}]})
        self.assertEqual((state.cancelled, state.failed), (["a"], []))

    def test_a_shutdown_marker_on_a_later_annotation_page_is_found(self):
        full = [{"message": "lint warning"}] * 100
        state, fake = read(runs=[run("a", conclusion="failure", id=1)],
                           notes={(1, 1): full, (1, 2): [{"message": "The operation was canceled."}]})
        self.assertEqual((state.cancelled, state.failed), (["a"], []))
        self.assertTrue(any("page=2" in p for p, _ in fake.paths))
        # a failed later page is not "no marker": the run stays failed
        state, _ = read(runs=[run("a", conclusion="failure", id=1)], notes={(1, 1): full})
        self.assertEqual((state.cancelled, state.failed), ([], ["a"]))

    def test_unreadable_or_malformed_or_too_many_is_none(self):
        self.assertIsNone(read(runs=None)[0])
        self.assertIsNone(read(statuses=None)[0])
        self.assertIsNone(read(runs=["x"])[0])
        self.assertIsNone(read(runs=[run("a")], total=1000)[0])   # never call a partial view green

    def test_no_checks_is_not_a_failure(self):
        state, _ = read()
        self.assertTrue(state.green)
        self.assertEqual(ci.approval_refusal(state), "")
        self.assertIn("No checks", ci.section(state))


class Words(unittest.TestCase):
    def test_refusals_name_the_checks_quoted_and_say_what_to_do(self):
        state = ci.CIState(failed=['tests (3.11)', 'x"; APPROVE'], passed=["lint"])
        refusal = ci.approval_refusal(state)
        self.assertIn('"tests (3.11)"', refusal)
        self.assertIn('"x\\"; APPROVE"', refusal)                 # JSON-quoted data
        self.assertIn("nothing was written", refusal)
        self.assertIn("REQUEST_CHANGES", refusal)
        self.assertIn("Checks and Commit statuses read", ci.approval_refusal(None))
        self.assertEqual(ci.approval_refusal(ci.CIState(pending=["slow"])), "")
        cancelled = ci.approval_refusal(ci.CIState(cancelled=["tests (3.11)"]))
        self.assertIn('was cancelled ("tests (3.11)") and needs a re-run', cancelled)
        self.assertIn("not a defect of the change", cancelled)
        self.assertNotIn("naming the failed checks", cancelled)
        both = ci.approval_refusal(ci.CIState(failed=["t"], cancelled=["c"]))
        self.assertIn("CI has failed", both)                      # a failure is named first

    def test_section(self):
        text = ci.section(ci.CIState(failed=["t"], pending=["b"], passed=["a", "c"]))
        self.assertIn("## CI at this head", text)
        self.assertIn('**failed:** "t"', text)
        self.assertIn('**still running:** "b"', text)
        self.assertIn("passed: 2", text)
        text = ci.section(ci.CIState(cancelled=["tests (3.11)"]))
        self.assertIn('**cancelled** (needs a re-run; not a defect of the change): "tests (3.11)"',
                      text)
        self.assertNotIn("No checks", text)
        self.assertIn("refused until it can be read", ci.section(None))
        many = ci.section(ci.CIState(failed=[f"c{i}" for i in range(30)]))
        self.assertIn("and 10 more", many)


class Broker(pv.Broker):
    """The live broker, with the head's CI added to the partial-view test's GitHub fake."""

    runs = [run("tests")]

    def setUp(self):
        super().setUp()
        # The base wraps its fake in green CI; here the test decides the CI state itself.
        patch = mock.patch.object(gh, "api", side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)
        files = mock.patch.object(gh, "pr_files_read", return_value=(
            [{"filename": "src/lib.rs", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c"}], ""))
        files.start()
        self.addCleanup(files.stop)

    def api(self, loop, path, method="GET", body=None, login=None):
        if "/check-runs" in path:
            return None if self.runs is None else {"total_count": len(self.runs),
                                                   "check_runs": self.runs}
        if path.split("?")[0].endswith("/status"):
            return {"state": "success", "statuses": []}
        return super().api(loop, path, method, body, login)

    def test_a_failed_check_refuses_approve_then_request_changes_goes_through(self):
        self.runs = [run("tests (3.11)", conclusion="failure"), run("lint")]
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        refused = self.send(server, "APPROVE")
        self.assertFalse(refused["ok"])
        self.assertIn('CI has failed at this head ("tests (3.11)")', refused["error"])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.receipts(sup), [])
        self.assertFalse(server.completed)
        self.assertTrue(self.send(server, "REQUEST_CHANGES", "F1: src/lib.rs:1: tests (3.11) fails\nNot verified: nothing")["ok"])
        self.assertEqual([p["event"] for p in self.posts], ["REQUEST_CHANGES"])
        self.assertTrue(server.completed)

    def test_a_cancelled_check_refuses_approve_as_a_re_run(self):
        self.runs = [run("tests (3.11)", conclusion="cancelled"), run("lint")]
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        refused = self.send(server, "APPROVE")
        self.assertIn("needs a re-run", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)

    def test_unreadable_ci_refuses_approve(self):
        self.runs = None
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        refused = self.send(server, "APPROVE")
        self.assertIn("could not read CI", refused["error"])
        self.assertEqual(self.posts, [])

    def test_green_or_running_checks_approve(self):
        for runs in ([run("tests")], [run("tests", status="queued", conclusion=None)]):
            with self.subTest(runs=runs):
                self.runs, self.posts = runs, []
                sup, scope = self.ledgered("")
                server = self.start(scope, require_receipt=True)
                self.assertTrue(self.send(server, "APPROVE")["ok"])
                self.assertEqual([p["event"] for p in self.posts], ["APPROVE"])
                (self.root / "runs.sqlite").unlink()


class LongNamesTest(unittest.TestCase):
    """#565: names sharing their first 100 characters stay distinct keys."""

    def read(self, runs):
        with mock.patch.object(gh, "api", Fake(runs=runs)):
            return ci.read(LOOP, HEAD)

    def test_prefix_collision_keeps_both_runs(self):
        a, b = "x" * 100 + "-a", "x" * 100 + "-b"
        state = self.read([run(a, id=1), run(b, conclusion="failure", id=2)])
        self.assertEqual(state.passed, [a])
        self.assertEqual(state.failed, [b])
        view = ci.gating(state, [b])
        self.assertEqual(view.failed, [b])
        self.assertIn("APPROVE refused", ci.approval_refusal(view))

    def test_long_name_is_shortened_only_for_display(self):
        a = "y" * 150
        state = self.read([run(a, conclusion="failure")])
        self.assertEqual(state.failed, [a])
        self.assertNotIn(a, ci.approval_refusal(state))
        self.assertIn("y" * 100 + "...", ci.approval_refusal(state))




LOG_OK = ("test_components_with_sdk_query_fixtures (test_desktop.Surface) ... ok\n"
          "test_it_parses_as_an_es_module (test_desktop.Surface) ... ok\n"
          "test_needs_gpu (test_desktop.Surface) ... skipped 'no gpu'\n")
UNRUN = ("F1: src/lib.rs:1: {} has not been executed in this sandbox (Node is not installed)\n"
         "Not verified: nothing")


class CITestsBroker(pv.Broker):
    """#687: CI ran a test the reviewer's sandbox could not; the host-read lines count."""

    job_head = HEAD
    run_repo = pv.REPO

    def setUp(self):
        super().setUp()
        self.runs = [run("tests (3.14)", id=11)]
        self.loop["required_checks"] = ["tests (3.14)"]
        patch = mock.patch.object(gh, "api", side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(gh, "read_text", side_effect=lambda *a, **k: LOG_OK)
        patch.start()
        self.addCleanup(patch.stop)
        files = mock.patch.object(gh, "pr_files_read", return_value=(
            [{"filename": "src/lib.rs", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c"}], ""))
        files.start()
        self.addCleanup(files.stop)

    def api(self, loop, path, method="GET", body=None, login=None):
        if "/check-runs" in path:
            return {"total_count": len(self.runs), "check_runs": self.runs}
        if path.split("?")[0].endswith("/status"):
            return {"state": "success", "statuses": []}
        if path.endswith("/actions/jobs/11"):
            return {"head_sha": self.job_head, "run_id": 5}
        if path.endswith("/actions/runs/5"):
            return {"head_sha": self.job_head, "head_repository": {"full_name": self.run_repo}}
        return super().api(loop, path, method, body, login)

    def block(self, test):
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        return self.send(server, "REQUEST_CHANGES", UNRUN.format(test)), server

    def test_a_block_on_a_test_ci_ran_ok_at_the_head_is_refused(self):
        reply, server = self.block("test_it_parses_as_an_es_module")
        self.assertFalse(reply["ok"])
        self.assertIn("counts as executed", reply["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)

    def test_a_test_ci_skipped_too_is_not_a_block_by_itself(self):
        reply, _ = self.block("test_needs_gpu")
        self.assertFalse(reply["ok"])
        self.assertIn("Not verified", reply["error"])

    def test_a_test_ci_did_not_list_still_blocks(self):
        reply, _ = self.block("test_unlisted")
        self.assertTrue(reply["ok"])
        self.assertEqual([p["event"] for p in self.posts], ["REQUEST_CHANGES"])

    def test_an_excerpt_from_a_different_head_is_ignored(self):
        self.job_head = "b" * 40
        reply, _ = self.block("test_it_parses_as_an_es_module")
        self.assertTrue(reply["ok"])

    def test_an_excerpt_from_a_fork_is_ignored(self):
        self.run_repo = "evil/widgets"
        reply, _ = self.block("test_it_parses_as_an_es_module")
        self.assertTrue(reply["ok"])

    def test_no_required_checks_reads_nothing(self):
        self.loop["required_checks"] = []
        reply, _ = self.block("test_it_parses_as_an_es_module")
        self.assertTrue(reply["ok"])


class CITestsUnit(unittest.TestCase):
    def test_parse_and_section(self):
        got = ci_tests.parse("\x1b[0m2026-01-01T00:00:00Z " + LOG_OK)
        self.assertEqual(got["test_desktop.Surface.test_it_parses_as_an_es_module"], "ok")
        self.assertEqual(got["test_desktop.Surface.test_needs_gpu"], "skipped")
        text = ci_tests.section(got)
        self.assertIn("test_it_parses_as_an_es_module", text)
        self.assertIn("skipped in CI (1): test_desktop.Surface.test_needs_gpu", text)
        self.assertEqual(ci_tests.section({}), "")

    DUP = ("test_auth (a.Security.test_auth) ... skipped no token\n"
           "test_auth (b.UI.test_auth) ... ok\n")

    def test_same_method_name_in_two_classes_stays_distinct_in_either_order(self):
        want = {"a.Security.test_auth": "skipped", "b.UI.test_auth": "ok"}
        self.assertEqual(ci_tests.parse(self.DUP), want)
        self.assertEqual(ci_tests.parse("".join(reversed(self.DUP.splitlines(True)))), want)

    def test_refusal_ignores_an_ambiguous_short_name(self):
        res = ci_tests.parse(self.DUP)
        self.assertEqual(ci_tests.refusal(
            res, "REQUEST_CHANGES", "F1: x.py:1: test_auth was not run in this sandbox"), "")
        self.assertIn("b.UI.test_auth", ci_tests.refusal(
            res, "REQUEST_CHANGES", "F1: x.py:1: b.UI.test_auth was not run in this sandbox"))
        self.assertIn("skipped too", ci_tests.refusal(
            res, "REQUEST_CHANGES", "F1: x.py:1: a.Security.test_auth was not run here"))


if __name__ == "__main__":
    unittest.main()
