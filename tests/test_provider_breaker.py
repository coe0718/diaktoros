"""#1155: a run of provider-killed turns holds the account, and an operator can reopen it.

Live 2026-10-20: DeepSeek answered `HTTP 400: This response_format type is unavailable now`
partway through every fixer turn for a night. #1325's hold knew only two DirectSDK messages, so
each issue spent four full attempts and the queue moved on to the next. Now the error the
inference proxy recorded counts: ``provider_breaker_failures`` of them in a row hold the account
as an outage does, and ``retry --clear-hold`` reopens it once the model is switched.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import io
import json
import pathlib
import sqlite3
import sys
import time
import unittest
from contextlib import closing, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, gate, pacing, retry_action  # noqa: E402
from diaktoros.cli import operations  # noqa: E402
from diaktoros.config import lists  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402
import test_pacing as tp  # noqa: E402

DEEPSEEK = "HTTP 400: This response_format type is unavailable now"


def killed_by(error: str):
    """A fake turn that exits 1 with ``error`` as the proxy's last provider error."""
    def run(_loop, scope, observed=None, **kw):
        observed.update(stdout="agent output", stderr="", provider_error=error)
        return 1
    return run


class Classify(unittest.TestCase):
    def test_the_providers_errors_count_and_the_proxys_own_refusal_does_not(self):
        for error in (DEEPSEEK, "HTTP 503: overloaded", "HTTP 429: slow down",
                      "upstream unreachable: ConnectionRefusedError: [Errno 111]"):
            with self.subTest(error=error):
                self.assertTrue(pacing.provider_failure(error))
        for error in ("proxy refused: request of 9 bytes over the 8-byte limit", "", None,
                      "Traceback: HTTP 400: in the turn's own words", "HTTP 400 no colon"):
            with self.subTest(error=error):
                self.assertFalse(pacing.provider_failure(error))


class Count(unittest.TestCase):
    """``pacing``'s count, at fixed times."""

    def setUp(self):
        self.key = pacing.account_key("deepseek", "https://api.deepseek.com", "fix")
        self.addCleanup(lambda: pacing.path().unlink(missing_ok=True))
        pacing.path().unlink(missing_ok=True)

    def test_the_limit_in_a_row_holds_as_an_outage_does(self):
        at = time.time()
        self.assertIsNone(pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at))
        self.assertIsNone(pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 1))
        self.assertIsNone(pacing.held(self.key, now=at + 2))
        until = pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 2)
        self.assertAlmostEqual(until, at + 2 + pacing.UNAVAILABLE_BASE_S, delta=1)
        _, reason = pacing.held(self.key, now=at + 3)
        self.assertEqual(reason, f"provider unavailable (3 turns in a row: {DEEPSEEK})")

    def test_after_the_hold_lapses_the_next_failure_holds_again_at_once_and_longer(self):
        at = time.time()
        for offset in range(3):
            first = pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + offset)
        probe = first + 1                                   # the hold has lapsed: a probe turn
        self.assertIsNone(pacing.held(self.key, now=probe))
        again = pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=probe)
        self.assertAlmostEqual(again, probe + 2 * pacing.UNAVAILABLE_BASE_S, delta=1)

    def test_a_turn_launched_before_the_hold_does_not_lengthen_it(self):
        at = time.time()
        for offset in range(3):
            until = pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + offset)
        self.assertEqual(pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 60), until)

    def test_another_failure_or_a_success_ends_the_run(self):
        for end in (pacing.reset_provider_failures, pacing.clear_unavailable):
            with self.subTest(end=end.__name__):
                at = time.time()
                pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at)
                pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 1)
                self.assertTrue(end(self.key))
                self.assertIsNone(pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 2))
                self.assertIsNone(pacing.held(self.key, now=at + 3))
                end(self.key)

    def test_a_count_left_for_the_forget_window_starts_over(self):
        at = time.time()
        pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at)
        pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=at + 1)
        later = at + 1 + pacing._UNAVAILABLE_FORGET_S
        self.assertIsNone(pacing.count_provider_failure(self.key, DEEPSEEK, 3, now=later))


class Turns(tp.ProductionBase):
    """Through the real ``_run_production``: the fixture's turns all draw on ``self.account``."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: pacing.path().unlink(missing_ok=True))

    def test_three_in_a_row_hold_the_account_and_the_third_spends_no_retry(self):
        for retries in (1, 2):
            row = self.run_production(killed_by(DEEPSEEK))
            self.assertEqual((row["state"], row["retries"]), ("waiting", retries), row["error"])
            self.assertIsNone(pacing.held(self.account))
        row = self.run_production(killed_by(DEEPSEEK))
        self.assertEqual((row["state"], row["retries"]), ("waiting", 2), row["error"])
        self.assertTrue(row["error"].startswith(
            f"held: fixer provider unavailable (3 turns in a row: {DEEPSEEK}"), row["error"])
        self.assertAlmostEqual(row["retry_at"] - time.time(), pacing.UNAVAILABLE_BASE_S, delta=5)
        self.assertIn("no retry spent", views.next_step(row))
        # The next queued run on that account waits with no model call.
        row = self.run_production(lambda *a, **k: self.fail("launched into a held account"),
                                  seat="reviewer", author="reviewer")
        self.assertEqual((row["state"], row["retries"]), ("waiting", 0))
        self.assertTrue(row["error"].startswith("held: reviewer provider unavailable ("))
        self.assertEqual(self.turns, 3)

    def test_failures_that_are_not_the_providers_never_count_and_end_the_run(self):
        self.run_production(killed_by(DEEPSEEK))
        self.run_production(killed_by(DEEPSEEK))
        self.run_production(killed_by("proxy refused: request over the limit"))
        row = self.run_production(killed_by(DEEPSEEK))
        self.assertEqual((row["state"], row["retries"]), ("failed", 4), row["error"])
        self.assertIsNone(pacing.held(self.account), "the proxy's refusal ended the run of them")
        self.assertEqual(json.loads(pacing.path().read_text())["provider_failures"][self.account]
                         ["count"], 1)

    def test_a_plain_failure_with_no_provider_error_ends_the_run_too(self):
        self.run_production(killed_by(DEEPSEEK))
        self.run_production(killed_by(DEEPSEEK))
        self.run_production(killed_by(""))
        self.assertNotIn(self.account, json.loads(pacing.path().read_text()).get(
            "provider_failures", {}))

    def test_a_turn_that_never_reached_the_provider_leaves_the_count(self):
        def no_sandbox(*a, **k):
            raise BlockingIOError(11, "bwrap: clone(CLONE_NEWUSER) failed")
        self.run_production(killed_by(DEEPSEEK))
        self.run_production(killed_by(DEEPSEEK))
        row = self.run_production(no_sandbox, launches=False)
        self.assertIn("BlockingIOError", row["error"])
        self.assertIsNone(pacing.held(self.account))
        row = self.run_production(killed_by(DEEPSEEK))
        self.assertTrue(row["error"].startswith("held: fixer provider unavailable (3 turns"),
                        row["error"])

    def test_zero_turns_it_off(self):
        loop = config.normalize({**self.raw, "provider_breaker_failures": 0})
        for retries in (1, 2, 3):
            row = self.run_production(killed_by(DEEPSEEK), loop=loop)
            self.assertEqual((row["state"], row["retries"]), ("waiting", retries), row["error"])
        self.assertIsNone(pacing.held(self.account))
        self.assertNotIn("provider_failures", json.loads(pacing.path().read_text())
                         if pacing.path().exists() else {})

    def test_the_loops_own_limit_is_used(self):
        loop = config.normalize({**self.raw, "provider_breaker_failures": 1})
        row = self.run_production(killed_by(DEEPSEEK), loop=loop)
        self.assertEqual((row["state"], row["retries"]), ("waiting", 0), row["error"])
        self.assertIn("1 turns in a row", row["error"])


class ClearHold(tp.ProductionBase):
    """``retry --clear-hold``: the seat's profile's holds go, and its held runs are due now."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: pacing.path().unlink(missing_ok=True))
        self.ledger = self.home / "state" / "diaktoros-runs.sqlite"

    def rows(self):
        with closing(sqlite3.connect(self.ledger)) as con:
            con.row_factory = sqlite3.Row
            return {row["seat"]: dict(row) for row in con.execute("SELECT * FROM runs")}

    def test_it_clears_the_profiles_holds_and_makes_its_held_runs_due(self):
        loop = config.normalize({**self.raw, "provider_breaker_failures": 1})
        self.run_production(killed_by(DEEPSEEK), loop=loop)              # fixer: held
        self.run_production(lambda *a, **k: self.fail("launched"), loop=loop,
                            seat="reviewer", author="reviewer")         # held on the same key
        other = pacing.account_key("anthropic", "https://api.anthropic.com", "rev")
        pacing.hold(other, time.time() + 3600, "anthropic answered 429")
        with closing(sqlite3.connect(self.ledger)) as con, con:      # an issue fix, held too
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,"
                        "updated,retry_at,error) SELECT 'if1','if1',repo,9,head,'issue_fixer',"
                        "'',state,created,updated,retry_at,replace(error,'held: fixer',"
                        "'held: issue_fixer') FROM runs WHERE seat='fixer'")
        late_429 = pacing.account_key("openai-codex", "https://elsewhere", "fix")
        pacing.hold(late_429, time.time() + 3600, "openai-codex answered 429")
        with mock.patch.object(gate, "resume_isolated", return_value=True) as resume:
            result = retry_action.clear_hold(loop, "issue_fixer", ledger=self.ledger)
        self.assertEqual(result["profile"], "fix")
        self.assertEqual(result["cleared"], sorted([self.account, late_429]))
        self.assertIsNone(pacing.held(self.account))
        self.assertIsNone(pacing.held(late_429), "a 429 on the profile goes too")
        self.assertIsNotNone(pacing.held(other), "another profile's window stays")
        self.assertNotIn(self.account, json.loads(pacing.path().read_text())["provider_failures"])
        rows = self.rows()
        self.assertEqual(result["rearmed"], 2)
        self.assertAlmostEqual(rows["fixer"]["retry_at"], time.time(), delta=5)
        self.assertAlmostEqual(rows["issue_fixer"]["retry_at"], time.time(), delta=5)
        self.assertGreater(rows["reviewer"]["retry_at"], time.time() + 600,
                           "the reviewer runs as profile rev: not this account's to re-arm")
        resume.assert_called_once_with(loop)
        self.assertEqual(result["worker"], "started")

    def test_a_daily_cap_hold_is_not_an_account_hold(self):
        loop = config.normalize({**self.raw, "seats": {
            **self.raw["seats"], "fixer": {"profile": "fix", "route": "f", "daily_turns": 1}}})
        self.run_production(lambda *a, **k: 1, loop=loop)                 # spends the day's turn
        row = self.run_production(lambda *a, **k: self.fail("over the cap"), loop=loop)
        self.assertIn("daily turn cap", row["error"])
        with mock.patch.object(gate, "resume_isolated", return_value=True) as resume:
            result = retry_action.clear_hold(loop, "fixer", ledger=self.ledger)
        self.assertEqual((result["cleared"], result["rearmed"]), ([], 0))
        resume.assert_not_called()

    def test_a_seat_with_no_profile_clears_nothing(self):
        unnamed = pacing.account_key("openai-codex", self.inference.upstream, "")
        pacing.hold(unnamed, time.time() + 3600, "openai-codex answered 429")
        with mock.patch.object(gate, "resume_isolated") as resume:
            result = retry_action.clear_hold(self.loop, "triage", ledger=self.ledger)
        self.assertEqual(result, {"profile": "", "cleared": [], "rearmed": 0, "worker": ""})
        self.assertIsNotNone(pacing.held(unnamed))
        resume.assert_not_called()

    def cli(self, *argv):
        args = argparse.Namespace(loop="widgets", pr=None, seat=None, clear_hold=False)
        for flag, value in zip(argv[::2], argv[1::2]):
            setattr(args, flag, value)
        out = io.StringIO()
        with mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.ledger), \
                mock.patch.object(gate, "resume_isolated", return_value=True), \
                redirect_stdout(out):
            rc = operations.cmd_retry(args)
        return rc, out.getvalue()

    def test_the_command(self):
        rc, out = self.cli("clear_hold", True)
        self.assertEqual(rc, 2)
        self.assertIn("--clear-hold needs --seat", out)
        rc, out = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("--pr is required", out)
        rc, out = self.cli("clear_hold", True, "seat", "triage")
        self.assertEqual(rc, 2)
        self.assertIn("the triage seat has no profile in loop widgets", out)
        rc, out = self.cli("clear_hold", True, "seat", "fixer")
        self.assertEqual(rc, 0, out)
        self.assertIn("no hold on profile fix: nothing to clear", out)
        pacing.hold_unavailable(self.account, "empty stream with no finish_reason")
        rc, out = self.cli("clear_hold", True, "seat", "fixer")
        self.assertEqual(rc, 0, out)
        self.assertIn("hold cleared: openai-codex as profile fix", out)
        self.assertIsNone(pacing.held(self.account))


class Setting(unittest.TestCase):
    def test_the_value_is_checked_and_zero_is_off_not_unset(self):
        self.assertIsNone(config.DEFAULTS["provider_breaker_failures"])
        self.assertEqual(config.provider_breaker_failures({}), 3)
        self.assertEqual(config.provider_breaker_failures({"provider_breaker_failures": 0}), 0)
        self.assertEqual(config.provider_breaker_failures({"provider_breaker_failures": 7}), 7)
        self.assertEqual(lists.check_provider_breaker_failures("0", "x"), 0)
        self.assertEqual(lists.check_provider_breaker_failures("20", "x"), 20)
        self.assertIsNone(lists.check_provider_breaker_failures(" ", "x"))
        for bad in (-1, 21, "x", True, 2.5):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                lists.check_provider_breaker_failures(bad, "x")

    def test_the_loop_file_is_checked(self):
        import test_review_after_ci as rac
        self.assertEqual(config.normalize(rac.raw(provider_breaker_failures=0))
                         ["provider_breaker_failures"], 0)
        self.assertIsNone(config.normalize(rac.raw())["provider_breaker_failures"])
        for bad in (-1, 21, "x", True):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize(rac.raw(provider_breaker_failures=bad))


class EveryPath(unittest.TestCase):
    """init (form and flag), set, show, apply and setup (#323), with test_shadow.Cli's harness."""
    import test_shadow as _shadow
    setUpClass = classmethod(_shadow.Cli.setUpClass.__func__)
    tearDownClass = classmethod(_shadow.Cli.tearDownClass.__func__)
    setUp, cli, init = _shadow.Cli.setUp, _shadow.Cli.cli, _shadow.Cli.init

    def value(self):
        return json.loads(self.file.read_text()).get("provider_breaker_failures")

    def test_every_path(self):
        rc, out = self.init(settings={"provider_breaker_failures": "5"})
        self.assertEqual((rc, self.value()), (0, 5), out)
        self.file.unlink()
        rc, out = self.init("--provider-breaker-failures", "0",
                            settings={"provider_breaker_failures": "5"})
        self.assertEqual((rc, self.value()), (0, 0), out)
        loop_id = self.file.stem
        rc, out = self.cli("show", "--loop", loop_id)
        self.assertIn(" off ", next(line for line in out.splitlines()
                                  if line.strip().startswith("provider_breaker_failures")))
        rc, out = self.cli("set", "--loop", loop_id, "--provider-breaker-failures", "4")
        self.assertEqual((rc, self.value()), (0, 4), out)
        rc, out = self.cli("set", "--loop", loop_id, "--provider-breaker-failures", "21")
        self.assertEqual(self.value(), 4, out)
        self.assertIn("provider_breaker_failures must be a whole number, 0-20", out)
        rc, out = self.cli("set", "--loop", loop_id, "--provider-breaker-failures", "0")
        self.assertEqual((rc, self.value()), (0, 0), out)
        rc, out = self.cli("apply", "--loop", loop_id, settings={"provider_breaker_failures": "6"})
        self.assertEqual((rc, self.value()), (0, 6), out)
        rc, out = self.cli("set", "--loop", loop_id, "--provider-breaker-failures", "")
        self.assertEqual((rc, self.value()), (0, None), out)
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--provider-breaker-failures", "2"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, loop_id, interactive=False)
        self.assertIn("--provider-breaker-failures=2", argv)


if __name__ == "__main__":
    unittest.main()
