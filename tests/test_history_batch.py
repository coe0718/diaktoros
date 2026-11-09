"""``hermes dk history --since DATE --out DIR``: every merged PR with a turn since a date, one
file each, exactly as ``--pr N`` prints it. On ``test_history``'s fixture: PR 7 merged (its
whole cycle), PR 8 still open, PR 6 worked on only before the window."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_history as th  # noqa: E402
from diaktoros import cli, gh, history_batch, ledger  # noqa: E402

SINCE = "2026-09-21"                               # th.T is 2026-09-21 14:13 UTC
OPEN = {"number": 8, "state": "open", "merged_at": None}


class Batch(fg.Base):
    # The fixture's helpers, borrowed so test_history's own tests do not run twice.
    run_row, insert, cycle = th.History.run_row, th.History.insert, th.History.cycle

    def setUp(self):
        super().setUp()
        self.db = th.run_supervisor.production_ledger()
        self.sup = th.run_supervisor.Supervisor(self.db)
        self.reads, self.out = [], self.root / "cycles"

    def other_pr(self, number, at):
        delivery = f"reviewer:{number}:{at}"
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.submit(delivery, fg.REPO, number, th.A, "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded', created=?, launch_intent=?, "
                        "finished=? WHERE delivery=?", (at, at, at + 60, delivery))

    def github(self):
        def fetch(_loop, path, *_a, **_k):
            self.reads.append(path)
            return (OPEN if path.endswith("/8") else th.PR), ""
        empty = mock.Mock(return_value=([], ""))
        return mock.patch.multiple(gh, fetch=fetch, reviews_read=empty, pr_commits_read=empty,
                                   issue_comments_read=empty)

    def run_cli(self, *argv):
        ctx = type("Ctx", (), {"register_cli_command": lambda s, n, h, setup, **k:
                               setattr(s, "setup", setup)})()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes dk")
        ctx.setup(parser)
        args = parser.parse_args(["history", *argv])
        out = io.StringIO()
        with self.github(), redirect_stdout(out):
            rc = args.func(args)
        return rc, out.getvalue()

    def test_every_merged_pr_since_the_date_gets_its_own_file(self):
        self.cycle()
        self.other_pr(8, th.T + 700)
        self.other_pr(6, th.T - 3 * 86400)             # before the window: not listed at all
        rc, out = self.run_cli("--since", SINCE, "--out", str(self.out), "--format", "json")
        self.assertEqual(rc, 0)
        self.assertIn("wrote pr-7.json (#7)", out)
        self.assertIn("skipped #8: not merged (open)", out)
        self.assertNotIn("#6", out)
        self.assertIn("1 cycle(s) written", out)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["pr-7.json"])
        data = json.loads((self.out / "pr-7.json").read_text())
        self.assertEqual((data["schema"], data["pr"], len(data["rounds"])), (1, 7, 2))
        rc, single = th.History.run_cli(self, "--format", "json")      # same as --pr 7 prints
        self.assertEqual((self.out / "pr-7.json").read_text(), single)

    def test_each_file_is_byte_for_byte_what_pr_n_prints(self):
        self.cycle()
        for fmt, suffix in (("markdown", "md"), ("text", "txt"), ("json", "json")):
            with self.subTest(fmt=fmt):
                rc, _out = self.run_cli("--since", SINCE, "--out", str(self.out), "--format", fmt)
                self.assertEqual(rc, 0)
                rc, single = th.History.run_cli(self, "--format", fmt)
                self.assertEqual((self.out / f"pr-7.{suffix}").read_text(), single)   # #2174

    def test_markdown_and_offline(self):
        self.cycle()
        self.other_pr(8, th.T + 700)
        rc, out = self.run_cli("--since", SINCE, "--out", str(self.out), "--format", "markdown",
                               "--offline")
        self.assertEqual(rc, 0)
        self.assertEqual(self.reads, [])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["pr-7.md", "pr-8.md"])
        self.assertIn("merge state not read, so every PR with a turn was written", out)
        self.assertTrue((self.out / "pr-7.md").read_text().startswith("# Review history of"))

    def test_nothing_in_the_window_writes_nothing(self):
        rc, out = self.run_cli("--since", "1d", "--out", str(self.out))
        self.assertEqual(rc, 0)
        self.assertIn("nothing written", out)
        self.assertFalse(self.out.exists())
        self.assertEqual(history_batch.prs_since(self.root / "absent.sqlite", fg.REPO, 0),
                         ([], "no run ledger"))

    def test_a_bad_flag_set_exits_2(self):
        for argv in ((), ("--pr", "7", "--since", "7d", "--out", "x"), ("--since", "7d"),
                     ("--pr", "7", "--out", "x"), ("--since", "soon", "--out", "x")):
            rc, out = self.run_cli(*argv)
            self.assertEqual(rc, 2, argv)
        self.assertIn("use 7d, 24h or a date", out)

    def test_an_out_that_is_a_file_exits_2(self):
        """#2175: a clean refusal, not a traceback, like the command's other refusals."""
        self.cycle()
        self.out.write_text("not a directory")
        rc, out = self.run_cli("--since", SINCE, "--out", str(self.out))
        self.assertEqual(rc, 2)
        self.assertIn(f"cannot write to {self.out}: ", out)


if __name__ == "__main__":
    unittest.main()
