"""#1790 ``prices``: the operator's per-model prices, and the cost ``stats`` estimates from them.

No provider the loop runs reports a cost, so ``cost`` is NULL on every turn. The setting is
checked by ``normalize``, written by init / setup / set / apply, shown by ``show``; ``stats``
prices each seat and model's tokens with it, says "unpriced" for a model with no entry (never
$0), and "at most" where some turns did not report their cache.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import config, stats  # noqa: E402
from diaktoros.cli import setup_command as cli_setup  # noqa: E402
from diaktoros.config import prices, settings_form  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
import run_tests as t  # noqa: E402
import test_human_paths as thp  # noqa: E402
import test_required_checks as trc  # noqa: E402

ENTRY = "deepseek-flash=0.14/0.014/0.28"
WANT = {"deepseek-flash": {"input": 0.14, "cached": 0.014, "output": 0.28}}


class Setting(unittest.TestCase):
    def test_normalize_checks_the_table(self):
        self.assertEqual(config.normalize(trc.raw())["prices"], {})
        self.assertEqual(config.normalize(trc.raw(prices={"DeepSeek-Flash": {
            "input": 0.14, "cached": 0.014, "output": 0.28}}))["prices"], WANT)
        good = {"input": 1, "cached": 0.1, "output": 2}
        for name, bad in (("not a map", ["x"]), ("a field missing", {"m": {"input": 1}}),
                          ("an extra field", {"m": {**good, "write": 1}}),
                          ("negative", {"m": {**good, "input": -1}}),
                          ("not finite", {"m": {**good, "output": float("nan")}}),
                          ("infinite", {"m": {**good, "output": float("inf")}}),
                          ("a bool", {"m": {**good, "cached": True}}),
                          ("text", {"m": {**good, "cached": "0.1"}}),
                          ("past the cap", {"m": {**good, "input": 10_001}}),
                          ("a space in the model", {"a b": good}),
                          ("one model twice", {"m": good, "M": good}),
                          ("no model", {" ": good})):
            with self.subTest(name), self.assertRaisesRegex(config.ConfigError, "'prices'"):
                config.normalize(trc.raw(prices=bad))

    def test_the_one_line_spelling_round_trips(self):
        table = prices.parse_prices(prices.split_prices(ENTRY + ", glm=1/0.2/3"), "t")
        self.assertEqual(table, {**WANT, "glm": {"input": 1.0, "cached": 0.2, "output": 3.0}})
        self.assertEqual(prices.prices_text(table), ENTRY + ", glm=1/0.2/3")
        for bad in ("no-equals", "m=1/2", "m=1/2/x", "m=1/2/3/4", "=1/2/3"):
            with self.subTest(bad), self.assertRaises(config.ConfigError):
                prices.parse_prices([bad], "t")
        with self.assertRaisesRegex(config.ConfigError, "twice"):
            prices.parse_prices(["m=1/1/1", "M=2/2/2"], "t")

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("prices", settings_form.apply_settings(trc.raw(), {}))
        self.assertEqual(settings_form.apply_settings(trc.raw(), {"prices": ENTRY})["prices"], WANT)
        self.assertEqual(settings_form.apply_settings(
            trc.raw(prices=WANT), {"prices": " "})["prices"], WANT)


class Cli(unittest.TestCase):
    setUpClass = classmethod(thp.Cli.setUpClass.__func__)
    tearDownClass = classmethod(thp.Cli.tearDownClass.__func__)
    setUp = thp.Cli.setUp
    cli = thp.Cli.cli
    init = thp.Cli.init

    def written(self) -> dict:
        return thp.json.loads(thp.LOOP_FILE.read_text()).get("prices", {})

    def test_init_from_the_form_and_the_flag_wins(self):
        glm = {"glm": {"input": 1.0, "cached": 0.2, "output": 3.0}}
        for extra, settings, want in (
                ((), None, {}), ((), {"prices": ENTRY}, WANT),
                (("--price", "glm=1/0.2/3"), {"prices": ENTRY}, glm)):
            with self.subTest(extra=extra, settings=settings):
                thp.LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_init_refuses_a_bad_entry(self):
        for bad in ("no-equals", "m=1/2", "m=-1/0/0"):
            with self.subTest(bad=bad):
                thp.LOOP_FILE.unlink(missing_ok=True)
                try:
                    rc, out = self.init("--price", bad)
                except Exception as exc:  # ConfigError escaping init is also a refusal
                    self.assertIn("prices", str(exc))
                else:
                    self.assertNotEqual(rc, 0, out)
                self.assertFalse(thp.LOOP_FILE.exists())

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--price", ENTRY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--price", "m=1/2")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--no-prices")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), {})
        rc, out = self.cli("apply", "--loop", thp.LOOP_ID, settings={"prices": ENTRY})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("show", "--loop", thp.LOOP_ID)
        self.assertIn(ENTRY, out)

    def test_setup_hands_init_each_entry(self):
        args = t.parser_for({"prices": ENTRY + ", glm=1/0.2/3"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--price")],
                         [f"--price={ENTRY}", "--price=glm=1/0.2/3"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--price", "c=1/1/1"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--price")], ["--price=c=1/1/1"])


class Estimate(unittest.TestCase):
    """Through the ledger: turns written by ``Supervisor.record_usage``, read by ``stats``."""
    REPO = "acme/widgets"

    def report(self, turns, table):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
            db = Path(d) / "runs.sqlite"
            sup = Supervisor(db)
            for n, (seat, model, observed) in enumerate(turns):
                sup.enqueue(f"d{n}", self.REPO, 7, "abcdef"[n] * 40, seat)   # one turn per head
                with closing(sqlite3.connect(db)) as con, con:
                    con.execute("UPDATE runs SET owner='w', created=?, model=? WHERE delivery=?",
                                (1000.0 + n, model, f"d{n}"))
                    run_id = con.execute("SELECT id FROM runs WHERE delivery=?",
                                         (f"d{n}",)).fetchone()[0]
                sup.record_usage(run_id, "w", {"cost": None, **observed})
            loop = {"repo": self.REPO, "id": "w", "prices": table}
            return stats.collect(loop, db, 0, with_github=False, now=86400)

    def test_cached_input_is_priced_apart_and_an_unpriced_model_is_said(self):
        report = self.report([
            ("shadow", "deepseek-flash", {"tokens_in": 1_000_000, "tokens_out": 100_000,
                                          "cache_read": 900_000}),
            ("reviewer", "gpt-6.1-terra", {"tokens_in": 500_000, "tokens_out": 1_000,
                                           "cache_read": 0})], WANT)
        rows = {row["seat"]: row for row in report["usage"]}
        # 100k uncached at 0.14, 900k cached at 0.014, 100k out at 0.28: $0.0546.
        self.assertAlmostEqual(rows["shadow"]["estimate"]["dollars"], 0.0546)
        self.assertFalse(rows["shadow"]["estimate"]["at_most"])
        self.assertIsNone(rows["reviewer"]["estimate"])
        text = stats.text(report)
        self.assertIn("$0.0546", text)
        self.assertIn("unpriced", text)
        self.assertNotIn("$0.0000", text)

    def test_a_turn_that_did_not_report_its_cache_makes_it_an_upper_bound(self):
        report = self.report([
            ("shadow", "deepseek-flash", {"tokens_in": 1_000_000, "tokens_out": 0,
                                          "cache_read": 1_000_000}),
            ("shadow", "deepseek-flash", {"tokens_in": 1_000_000, "tokens_out": 0,
                                          "cache_read": None})], WANT)
        (row,) = report["usage"]
        # The unreported turn's input is priced uncached: 1M at 0.014 plus 1M at 0.14.
        self.assertAlmostEqual(row["estimate"]["dollars"], 0.154)
        self.assertTrue(row["estimate"]["at_most"])
        self.assertIn("at most $0.1540", stats.text(report))

    def test_an_upper_bound_is_shown_rounded_up_never_below_the_bound(self):
        # 100 input tokens of unknown cache at 0.14/M is $0.000014: not "at most $0.0000".
        report = self.report([
            ("shadow", "deepseek-flash", {"tokens_in": 100, "tokens_out": 0,
                                          "cache_read": None})], WANT)
        (row,) = report["usage"]
        self.assertTrue(row["estimate"]["at_most"])
        self.assertGreater(row["estimate"]["dollars"], 0)
        text = stats.text(report)
        self.assertIn("at most $0.0001", text)
        self.assertNotIn("$0.0000", text)

    def test_estimate_text_rounds_only_upper_bounds_up(self):
        def shown(dollars, at_most):
            return prices.estimate_text({"dollars": dollars, "at_most": at_most})
        self.assertEqual(shown(1.4e-05, True), "at most $0.0001")
        self.assertEqual(shown(0.00012, True), "at most $0.0002")
        self.assertEqual(shown(0.0003, True), "at most $0.0003")     # exact: not bumped
        self.assertEqual(shown(0.0, True), "at most $0.0000")
        self.assertEqual(shown(1234.56781, True), "at most $1,234.5679")
        self.assertEqual(shown(0.00012, False), "$0.0001")           # an estimate rounds to nearest

    def test_an_upper_bound_keeps_a_real_fraction_above_a_boundary(self):
        # Rounding the scaled value to 6 places before ceil dropped these (#1831 review F1).
        def shown(row, table):
            return prices.estimate_text(prices.estimate(row, table))
        unknown = {"model": "m", "cached_of": 0, "cache_read": 0}
        over = {"m": {"input": 0.1, "cached": 0.01, "output": 0.00001}}
        tiny = {"m": {"input": 0.00001, "cached": 0.000001, "output": 0.0}}
        row = {**unknown, "tokens_in": 1000, "tokens_out": 1}
        self.assertGreater(prices.estimate(row, over)["dollars"], 0.0001)
        self.assertEqual(shown(row, over), "at most $0.0002")
        row = {**unknown, "tokens_in": 1, "tokens_out": 0}
        self.assertGreater(prices.estimate(row, tiny)["dollars"], 0)
        self.assertEqual(shown(row, tiny), "at most $0.0001")
        # And through stats.text, the one caller.
        report = self.report([("shadow", "deepseek-flash", {"tokens_in": 1, "tokens_out": 0,
                                                            "cache_read": None})],
                             {"deepseek-flash": tiny["m"]})
        self.assertIn("at most $0.0001", stats.text(report))
        self.assertNotIn("$0.0000", stats.text(report))

    def test_estimate_text_forgives_only_float_noise_at_a_boundary(self):
        def shown(dollars):
            return prices.estimate_text({"dollars": dollars, "at_most": True})
        self.assertEqual(shown(0.0001 * 3), "at most $0.0003")
        self.assertEqual(shown(0.00010000001), "at most $0.0002")
        self.assertEqual(shown(1e-11), "at most $0.0001")


if __name__ == "__main__":
    unittest.main()
