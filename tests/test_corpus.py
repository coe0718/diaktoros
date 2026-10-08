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

    def test_malformed_case_is_refused_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "a.json").write_text(json.dumps(CASE))
            self.assertEqual(len(corpus.load(d)), 1)
            (d / "b.json").write_text(json.dumps({"id": "b", "pr": 1, "findings": [{"id": "x", "pattern": "("}]}))
            with self.assertRaises(corpus.CorpusError):
                corpus.load(d)


if __name__ == "__main__":
    unittest.main()
