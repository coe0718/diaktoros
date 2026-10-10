"""Golden corpus (#491): caught/missed per case, scores recorded per revision and model."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import corpus  # noqa: E402

CASE = {"id": "c1", "pr": 7, "findings": [{"id": "sql", "pattern": "sql injection"},
                                          {"id": "race", "pattern": r"race\s+condition"}]}


def RC(body):
    return {"verdict": "REQUEST_CHANGES", "body": body}


class Corpus(unittest.TestCase):
    def test_reports_caught_and_missed_per_case(self):
        out = corpus.replay([CASE], lambda c: {"verdict": "REQUEST_CHANGES",
                                               "body": "F1: SQL injection in query builder"})
        self.assertEqual(out, [{"case": "c1", "verdict": "REQUEST_CHANGES", "caught": ["sql"],
                                "missed": ["race"]}])

    def test_a_turn_that_fails_misses_everything(self):
        def boom(case):
            raise RuntimeError("no verdict")
        out = corpus.replay([CASE], boom)
        self.assertEqual(out[0]["missed"], ["sql", "race"])
        self.assertIn("no verdict", out[0]["error"])

    def test_scores_recorded_per_revision_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "scores.jsonl"
            results = corpus.replay([CASE], lambda c: RC("F1: race condition"))
            corpus.record(path, "rev1", "m1", results, now=1)
            corpus.record(path, "rev2", "m1", corpus.replay([CASE], lambda c: RC("F1: sql injection\nF2: race condition")), now=2)
            rows = corpus.history(path)
        self.assertEqual([(r["prompt_rev"], r["model"], r["caught"], r["missed"]) for r in rows],
                         [("rev1", "m1", 1, 1), ("rev2", "m1", 2, 0)])

    def test_corrupt_score_line_is_skipped_and_reported(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest import mock
        from diaktoros import cli
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            path = d / corpus.SCORES_FILE
            corpus.record(path, "rev1", "m1", corpus.replay([CASE], lambda c: RC("F1: race condition")), now=1)
            with open(path, "a") as f:
                f.write('{"at": 2, "prompt_rev": "re\n')
            bad = []
            self.assertEqual(len(corpus.history(path, bad)), 1)
            self.assertEqual(bad, [2])
            out = io.StringIO()
            args = SimpleNamespace(loop="x", dir=None, history=True)
            with mock.patch.object(cli.config, "load_id", return_value={}), \
                    mock.patch.object(corpus, "corpus_dir", return_value=d), \
                    mock.patch.object(corpus, "load", return_value=[]), \
                    mock.patch.object(cli.config, "state_dir", return_value=d), \
                    redirect_stdout(out):
                rc = cli.cmd_corpus(args)
        self.assertEqual(rc, 0)
        self.assertIn("rev1  m1  caught 1  missed 1", out.getvalue())
        self.assertIn("skipped 1 unreadable", out.getvalue())

    def test_invalid_utf8_score_line_keeps_valid_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / corpus.SCORES_FILE
            corpus.record(path, "rev1", "m1", corpus.replay([CASE], lambda c: RC("F1: race condition")), now=1)
            with open(path, "ab") as f:
                f.write(b'{"prompt_rev": "\xff"}\n')
            bad = []
            rows = corpus.history(path, bad)
            self.assertEqual(len(rows), 1)
            self.assertEqual(bad, [2])

    def test_malformed_case_is_refused_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "a.json").write_text(json.dumps(CASE))
            self.assertEqual(len(corpus.load(d)), 1)
            (d / "b.json").write_text(json.dumps({"id": "b", "pr": 1, "findings": [{"id": "x", "pattern": "("}]}))
            with self.assertRaises(corpus.CorpusError):
                corpus.load(d)

    def test_non_string_finding_fields_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "a.json").write_text(json.dumps({"id": "a", "pr": 1, "findings": [{"id": "x", "pattern": 5}]}))
            with self.assertRaises(corpus.CorpusError):
                corpus.load(d)


class Scoring(unittest.TestCase):
    def test_a_mention_in_an_approve_is_a_miss(self):
        body = "F1: I looked at the SQL injection and it is fine\nAPPROVE"
        out = corpus.score(CASE, {"verdict": "APPROVE", "body": body})
        self.assertEqual(out["caught"], [])

    def test_prose_outside_a_numbered_finding_is_a_miss(self):
        out = corpus.score(CASE, {"verdict": "REQUEST_CHANGES",
                                  "body": "I looked at the sql injection (fine)."})
        self.assertEqual(out["caught"], [])

    def test_request_changes_with_the_pattern_in_a_finding_line_is_a_catch(self):
        out = corpus.score(CASE, {"verdict": "REQUEST_CHANGES",
                                  "body": "F1: db.py: SQL injection in the builder"})
        self.assertEqual(out["caught"], ["sql"])
        self.assertEqual(out["verdict"], "REQUEST_CHANGES")

    def test_head_must_be_a_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "a.json").write_text(json.dumps({**CASE, "head": 5}))
            with self.assertRaises(corpus.CorpusError):
                corpus.load(d)

    def test_seed_cases_load(self):
        cases = corpus.load(ROOT / "docs" / "corpus")
        self.assertEqual(sorted(c["pr"] for c in cases), [460, 497, 503, 506])


LOOP = {"repo": "o/r", "id": "l", "read_token": "t", "cap": 3, "seats": {}, "reviewers": ["rev"],
        "reviewer_seat": "rev"}
LEAK = "EARLIER-VERDICT-SECRET-FINDING"


class LiveReview(unittest.TestCase):
    """Real ``pr_change`` and ``isolated_prompt``; only GitHub reads and the turn are mocked."""

    def _run(self, case, submissions):
        from unittest import mock
        from diaktoros import ci, ci_fix, gh, issue_facts, selftest, trusted_turn
        seen = {}

        def fake_turn(loop, scope, **kw):
            seen["scope"], seen["kw"] = scope, kw
            kw["observed"]["submissions"] = submissions
        reviewer = mock.Mock(upstream="u", key="k", model="m", api_mode="a",
                             proxy_model="pm", client_identity="ci")
        reviewer.credential_provider.return_value = "cred"
        settings = {"source": "/s", "venv": "/v", "runtime": "/r", "rust": "/x"}
        pr = {"number": 7, "changed_files": 0, "head": {"sha": "TIP", "ref": "br"},
              "base": {"ref": "main", "sha": "b" * 40}, "title": "t", "body": "b",
              "user": {"login": "u"}}
        earlier = [{"user": {"login": "rev"}, "state": "CHANGES_REQUESTED", "body": LEAK,
                    "commit_id": "TIP", "submitted_at": "2026-10-01T00:00:00Z"}]
        with mock.patch.object(gh, "fetch", return_value=(pr, None)), \
                mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "pr_files_read", return_value=([], "")), \
                mock.patch.object(gh, "reviews", return_value=earlier), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(gh, "pr_url", return_value="https://github.com/o/r/pull/7"), \
                mock.patch.object(ci, "read", return_value=ci.CIState(passed=["t"])), \
                mock.patch.object(ci_fix, "fix_history", return_value=""), \
                mock.patch.object(ci_fix, "still_red", return_value=""), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(selftest, "_work_root", return_value="/w"), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=fake_turn):
            review = corpus.live_review(LOOP, settings, reviewer, 60)
            return review(case), seen

    def test_turn_runs_no_write_and_returns_the_last_review(self):
        body, seen = self._run({"id": "c", "pr": 7, "head": "TIP", "findings": []},
                               [{"verdict": "APPROVE", "body": "first"},
                                {"verdict": "REQUEST_CHANGES", "body": "F1: SQL injection"}])
        self.assertEqual(body, {"verdict": "REQUEST_CHANGES", "body": "F1: SQL injection"})
        self.assertIs(seen["kw"]["no_write"], True)
        self.assertEqual(seen["scope"].head, "TIP")

    def test_prompt_carries_no_earlier_review(self):
        _, seen = self._run({"id": "c", "pr": 7, "findings": []}, [{"body": "x"}])
        self.assertNotIn(LEAK, seen["kw"]["prompt"])
        self.assertIn("round", seen["kw"]["prompt"])

    def test_a_case_at_an_earlier_head_is_refused_not_missed(self):
        # replay() must not turn it into an all-missed result.
        case = {"id": "c", "pr": 7, "head": "OLD", "findings": [{"id": "x", "pattern": "x"}]}
        with self.assertRaises(corpus.UnsupportedCase) as ctx:
            corpus.replay([case], lambda c: self._run(c, [{"body": "x"}])[0])
        self.assertIn("final head", str(ctx.exception))

    def test_check_heads_refuses_an_earlier_head_before_any_replay(self):
        from unittest import mock
        from diaktoros import gh
        case = {"id": "c", "pr": 7, "head": "OLD", "findings": []}
        with mock.patch.object(gh, "fetch", return_value=({"head": {"sha": "TIP"}}, None)):
            with self.assertRaises(corpus.UnsupportedCase):
                corpus.check_heads(LOOP, [case])
            corpus.check_heads(LOOP, [{**case, "head": "TIP"}])

    def test_no_submission_raises(self):
        with self.assertRaises(corpus.CorpusError):
            self._run({"id": "c", "pr": 7, "findings": []}, [])


if __name__ == "__main__":
    unittest.main()
