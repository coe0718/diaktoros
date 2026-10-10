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


class Corpus(unittest.TestCase):
    def test_reports_caught_and_missed_per_case(self):
        out = corpus.replay([CASE], lambda c: "P1: SQL injection in query builder")
        self.assertEqual(out, [{"case": "c1", "caught": ["sql"], "missed": ["race"]}])

    def test_a_turn_that_fails_misses_everything(self):
        def boom(case):
            raise RuntimeError("no verdict")
        out = corpus.replay([CASE], boom)
        self.assertEqual(out[0]["missed"], ["sql", "race"])
        self.assertIn("no verdict", out[0]["error"])

    def test_scores_recorded_per_revision_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "scores.jsonl"
            results = corpus.replay([CASE], lambda c: "race condition")
            corpus.record(path, "rev1", "m1", results, now=1)
            corpus.record(path, "rev2", "m1", corpus.replay([CASE], lambda c: "sql injection race condition"), now=2)
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
            corpus.record(path, "rev1", "m1", corpus.replay([CASE], lambda c: "race condition"), now=1)
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


class LiveReview(unittest.TestCase):
    def _run(self, case, submissions):
        from unittest import mock
        from diaktoros import gh, run_supervisor, selftest, trusted_turn
        change = mock.Mock(diff="THE DIFF")
        seen = {}

        def fake_turn(loop, scope, **kw):
            seen["scope"], seen["kw"] = scope, kw
            kw["observed"]["submissions"] = submissions
        reviewer = mock.Mock(upstream="u", key="k", model="m", api_mode="a",
                             proxy_model="pm", client_identity="ci")
        reviewer.credential_provider.return_value = "cred"
        settings = {"source": "/s", "venv": "/v", "runtime": "/r", "rust": "/x"}
        loop = {"repo": "o/r"}
        with mock.patch.object(gh, "fetch", return_value=({"head": {"sha": "TIP", "ref": "br"}}, None)), \
                mock.patch.object(gh, "reviews", return_value=[]), \
                mock.patch.object(run_supervisor, "effective_reviews", return_value=[]), \
                mock.patch.object(run_supervisor, "pr_change", return_value=change) as pc, \
                mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
                mock.patch.object(selftest, "ledger_path", return_value="/l"), \
                mock.patch.object(selftest, "_work_root", return_value="/w"), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=fake_turn):
            review = corpus.live_review(loop, settings, reviewer, 60)
            try:
                body = review(case)
            finally:
                seen["row"] = pc.call_args[0][1] if pc.call_args else None
        return body, seen

    def test_turn_runs_no_write_at_the_case_head_and_returns_last_body(self):
        body, seen = self._run({"id": "c", "pr": 7, "head": "OLD", "findings": []},
                               [{"body": "first"}, {"body": "SQL injection"}])
        self.assertEqual(body, "SQL injection")
        self.assertIs(seen["kw"]["no_write"], True)
        self.assertEqual(seen["scope"].head, "OLD")
        self.assertEqual(seen["row"]["head"], "OLD")
        self.assertEqual(seen["kw"]["review_diff"], "THE DIFF")
        self.assertEqual(seen["kw"]["prompt"], "PROMPT")

    def test_head_defaults_to_the_prs_current_head(self):
        _, seen = self._run({"id": "c", "pr": 7, "findings": []}, [{"body": "x"}])
        self.assertEqual(seen["scope"].head, "TIP")

    def test_no_submission_raises(self):
        with self.assertRaises(corpus.CorpusError):
            self._run({"id": "c", "pr": 7, "findings": []}, [])


if __name__ == "__main__":
    unittest.main()
