"""#1790 ``prices``: the operator's per-model prices, and the cost ``stats`` estimates from them.

No provider the loop runs reports a cost, so ``cost`` is NULL on every turn. The setting is
checked by ``normalize``, written by init / setup / set / apply, shown by ``show``; ``stats``
prices each seat and model's tokens with it, says "unpriced" for a model with no entry (never
$0), and "at most" or "at least", rounded outward, where some turns did not report their cache.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import decimal
import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
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

    def test_what_show_prints_reads_back_as_the_same_table(self):
        # #1827: six significant digits turned 0.123456789 into 0.123457.
        table = prices.check_prices({"m": {"input": 0.123456789, "cached": 1.23456789e-08,
                                           "output": 1.123456789},
                                     "n": {"input": 3, "cached": 0.3, "output": 15}}, "t")
        text = prices.prices_text(table)
        self.assertEqual(prices.parse_prices(prices.split_prices(text), "t"), table)
        self.assertIn("n=3/0.3/15", text)                       # whole numbers stay plain

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
        self.assertIsNone(rows["shadow"]["estimate"]["bound"])
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
        self.assertEqual(row["estimate"]["bound"], "at most")
        self.assertIn("at most $0.1540", stats.text(report))

    def test_the_bound_is_on_the_side_the_prices_put_it(self):
        # #1826: with cached input dearer than uncached (1/2/0), pricing an unreported cache as
        # uncached is a floor: $1 for 1M tokens that could have cost $2 cached.
        row = {"model": "m", "tokens_in": 1_000_000, "tokens_out": 0, "cache_read": 0,
               "cached_of": 0}
        for cached, bound in ((0.5, "at most"), (2.0, "at least"), (1.0, None)):
            with self.subTest(cached=cached):
                got = prices.estimate(row, {"m": {"input": 1.0, "cached": cached, "output": 0}})
                self.assertEqual((got["dollars"], got["bound"]), (1.0, bound))
        self.assertEqual(prices.estimate_text({"dollars": 1.0, "bound": "at least"}),
                         "at least $1.0000")
        reported = {**row, "cached_of": 1_000_000}               # every turn reported: exact
        self.assertIsNone(prices.estimate(reported, {"m": {"input": 1, "cached": 2,
                                                           "output": 0}})["bound"])

    def test_a_bound_rounds_away_from_what_it_bounds(self):
        # #1832: 1M tokens whose cache went unreported could all have been uncached, so the
        # all-uncached cost is what the bound is about. Rounded to nearest, $1.00006 showed as
        # "at least $1.0001" and $1.00004 as "at most $1.0000": both false.
        row = {"model": "m", "tokens_in": 1_000_000, "tokens_out": 0, "cache_read": 0,
               "cached_of": 0}
        for price, cached, want in ((1.00006, 2.0, "at least $1.0000"),
                                    (1.00004, 0.5, "at most $1.0001"),
                                    (1.00006, 1.00006, "$1.0001")):     # exact: to nearest
            with self.subTest(price=price, cached=cached):
                table = {"m": {"input": price, "cached": cached, "output": 0}}
                self.assertEqual(prices.estimate_text(prices.estimate(row, table)), want)
        # The sum is in decimal, each price as written: in floats 700k at 0.07 plus 300k at 0.01
        # is $0.052000000000000005, which a ceiling would show as $0.0521.
        split = {**row, "cache_read": 300_000}
        table = {"m": {"input": 0.07, "cached": 0.01, "output": 0}}
        self.assertEqual(prices.estimate_text(prices.estimate(split, table)), "at most $0.0520")

    def test_stats_prints_the_bound_rounded_outward(self):
        report = self.report([
            ("shadow", "m", {"tokens_in": 1_000_000, "tokens_out": 0, "cache_read": None})],
            {"m": {"input": 1.00004, "cached": 0.5, "output": 0}})
        self.assertIn("at most $1.0001", stats.text(report))

    def test_no_price_is_too_small_to_move_a_bound(self):
        # #1832 round 2: summed to 80 digits, 1M tokens (one cached at 5e-324, the least price
        # accepted) plus one output token lost the 5e-330 and showed "at most $1.0000" for a
        # cost of $1 and 5e-330. The sum is exact at any accepted price.
        row = {"model": "m", "tokens_in": 1_000_000, "tokens_out": 1, "cache_read": 1,
               "cached_of": 0}
        got = prices.estimate(row, {"m": {"input": 1, "cached": 5e-324, "output": 1}})
        self.assertGreater(Decimal(got["exact"]), 1)
        self.assertEqual(prices.estimate_text(got), "at most $1.0001")

    def test_the_json_dollars_is_on_the_bounds_side(self):
        # #1832 rounds 2-3: "dollars" with its "bound" must hold as stats' JSON is read, as a
        # float (the nearest float to 1.0000000000000001 is 1.0, below it) and as the decimal
        # text written (the float nearest 0.100...001 is above it, but is written "0.1"). The
        # cached price only sets the bound's side: the turns' cache went unreported.
        cases = (("at most", 0.5, 1.0000000000000002, 1.0, 500_000, 500_000),
                 ("at least", 2.0, 1.0000000000000004, 1.0, 500_000, 500_000),
                 ("at most", 0.05, 0.1, 0.10000000000000002, 950_000, 50_000),
                 ("at least", 1.0, 0.3, 0.2999999999999999, 999_999, 1))
        for bound, cached, price, out_price, tokens_in, tokens_out in cases:
            with self.subTest(bound=bound, price=price, out_price=out_price):
                turn = {"tokens_in": tokens_in, "tokens_out": tokens_out, "cache_read": None}
                table = {"m": {"input": price, "cached": cached, "output": out_price}}
                text = stats.as_json(self.report([("shadow", "m", turn)], table))
                cost = (tokens_in * Decimal(repr(price))
                        + tokens_out * Decimal(repr(out_price))) / 1_000_000   # all uncached
                got = json.loads(text)["usage"][0]["estimate"]
                self.assertEqual(got["bound"], bound)
                self.assertEqual(Decimal(got["exact"]), cost)
                literal = json.loads(text, parse_float=Decimal)["usage"][0]["estimate"]
                for read in (Decimal(got["dollars"]), literal["dollars"]):   # binary, then text
                    if bound == "at most":
                        self.assertGreaterEqual(read, cost)
                    else:
                        self.assertLessEqual(read, cost)

    def test_every_bound_holds_read_either_way(self):
        # The guarantee itself, over 3,000 seeded random tables and token counts (prices from
        # the least float to PRICE_MAX, most near round figures): the exact sum is the
        # all-uncached cost, and "dollars" is on the bound's side read as binary or as JSON text.
        import random
        rng = random.Random(1832)

        def a_price():
            pick = rng.random()
            if pick < 0.1:
                return 5e-324
            base = float(f"{rng.uniform(0, 3):.{rng.randint(1, 4)}f}")
            for _ in range(rng.randint(0, 3)):
                base = math.nextafter(base, rng.choice((0, prices.PRICE_MAX)))
            return min(max(base, 0.0), prices.PRICE_MAX) if pick < 0.9 else prices.PRICE_MAX
        for _ in range(3000):
            price, out_price = a_price(), a_price()
            cached = rng.choice((price / 2, price * 2 + 1))
            tokens_in, tokens_out = rng.randint(1, 10 ** 9), rng.randint(0, 10 ** 7)
            row = {"model": "m", "tokens_in": tokens_in, "tokens_out": tokens_out,
                   "cache_read": 0, "cached_of": 0}
            got = prices.estimate(row, {"m": {"input": price, "cached": cached,
                                              "output": out_price}})
            cost = Decimal(got["exact"])
            with decimal.localcontext(prec=1000):    # the expected sum, unrounded too
                self.assertEqual(cost, (tokens_in * Decimal(repr(price))
                                        + tokens_out * Decimal(repr(out_price))) / 10 ** 6)
            text = json.loads(json.dumps(got), parse_float=Decimal)["dollars"]
            for read in (Decimal(got["dollars"]), text):
                if got["bound"] == "at most":
                    self.assertGreaterEqual(read, cost, (price, out_price, tokens_in, tokens_out))
                elif got["bound"] == "at least":
                    self.assertLessEqual(read, cost, (price, out_price, tokens_in, tokens_out))

if __name__ == "__main__":
    unittest.main()
