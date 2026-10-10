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
from diaktoros import ci, gh  # noqa: E402
import test_partial_view_no_approve as pv  # noqa: E402

HEAD = pv.HEAD
LOOP = {"repo": pv.REPO, "read_token": "read"}


def run(name, status="completed", conclusion="success", id=1):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion}


class Fake:
    """Check runs and statuses for HEAD; ``None`` makes that read fail."""

    def __init__(self, runs=(), statuses=(), total=None, notes=None):
        self.runs, self.statuses, self.total = runs, statuses, total
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
        if path.endswith("/status"):
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
        if path.endswith("/status"):
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


if __name__ == "__main__":
    unittest.main()
