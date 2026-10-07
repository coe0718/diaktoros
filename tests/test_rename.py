"""The rename to Diaktoros (#425, stage 1): new names everywhere a person sees one, old ones still read.

`hermes diaktoros`, its short name `hermes dk`, and — for a release — the old `hermes review-loop`,
which runs the same verbs and says once that it was renamed. What the loop wrote under the old
name stays readable: an old footer is still stripped from review history, an old gate shim is
still ours (stale, so rewritten, never refused as foreign), and a loop naming the old qualified
skill is read as the new one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import attribution, cli, config, gate_shims  # noqa: E402

OLD_FOOTER = ("\n\n---\n<sub>🤖 Automated by [hermes-review-loop]"
              "(https://github.com/coe0718/hermes-review-loop) · reviewer seat · head `abc1234`</sub>")


class Ctx:
    def __init__(self):
        self.commands: dict = {}

    def register_cli_command(self, name, summary, setup, **kwargs):
        self.commands[name] = setup


def parse(ctx: Ctx, name: str, *argv: str):
    parser = argparse.ArgumentParser(prog=f"hermes {name}")
    ctx.commands[name](parser)
    return parser.parse_args(list(argv))


class Commands(unittest.TestCase):
    def setUp(self):
        saved = cli._PARSER
        self.addCleanup(setattr, cli, "_PARSER", saved)
        cli._PARSER = None
        self.ctx = Ctx()
        cli.register_cli(self.ctx, settings={})

    def test_three_names_with_the_full_one_registered_last(self):
        self.assertEqual(list(self.ctx.commands), ["review-loop", "dk", "diaktoros"])

    def run_verb(self, name):
        args = parse(self.ctx, name, "list")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(config, "all_loops", return_value=[]):
            args.func(args)
        return err.getvalue()

    def test_the_old_name_runs_the_same_verbs_and_says_it_was_renamed(self):
        self.assertIn(cli.RENAMED_NOTE, self.run_verb("review-loop"))
        for name in ("dk", "diaktoros"):
            self.assertNotIn(cli.RENAMED_NOTE, self.run_verb(name))
        bare = parse(self.ctx, "review-loop")
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            bare.func(bare)
        self.assertIn(cli.RENAMED_NOTE, err.getvalue())

    def test_the_old_name_never_becomes_the_parser_verbs_run_through(self):
        parse(self.ctx, "diaktoros")
        real = cli._PARSER
        parse(self.ctx, "review-loop")
        self.assertIs(cli._PARSER, real)


class Signature(unittest.TestCase):
    def test_signs_as_diaktoros(self):
        signed = attribution.stamp({}, "body", seat="reviewer", head="a" * 40)
        self.assertIn("Automated by [Diaktoros](https://github.com/coe0718/diaktoros)", signed)
        self.assertIn("Automated-By: Diaktoros (https://github.com/coe0718/diaktoros)",
                      attribution.sign_commit({}, "Fix it"))

    def test_history_loses_the_old_footer_and_the_new_one_but_nothing_else(self):
        self.assertEqual(attribution.unsign("body" + OLD_FOOTER), "body")
        self.assertEqual(attribution.unsign(attribution.stamp({}, "body", seat="fixer",
                                                              head="b" * 40)), "body")
        mixed = OLD_FOOTER.replace("(https://github.com/coe0718/hermes-review-loop)",
                                   "(https://example.com/x)")
        self.assertEqual(attribution.unsign("body" + mixed), "body" + mixed)


class Shims(unittest.TestCase):
    def test_an_old_marker_shim_is_ours_and_stale(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            home = pathlib.Path(tmp)
            (home / "scripts").mkdir()
            shim = home / "scripts" / "gate_reviewer.py"
            new = gate_shims.render("gate_reviewer.py")
            shim.write_text(new.replace(gate_shims.MARKER, gate_shims.OLD_MARKERS[0], 1))
            self.assertEqual(gate_shims.state(home, "gate_reviewer.py")[0], "stale")
            shim.write_text(new)
            self.assertEqual(gate_shims.state(home, "gate_reviewer.py")[0], "ok")
            shim.write_text("#!/usr/bin/env python3\n# someone else's script\n")
            self.assertEqual(gate_shims.state(home, "gate_reviewer.py")[0], "foreign")


class Skill(unittest.TestCase):
    def raw(self, skill):
        return {"repo": "acme/w", "fixers": ["fix"], "reviewers": ["rev"], "read_token": "reader",
                "tokens": {}, "skill": skill,
                "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-review"},
                          "fixer": {"profile": "f", "login": "fix", "route": "w-fix"}}}

    def test_the_old_qualified_skill_reads_as_the_new_name(self):
        self.assertEqual(config.normalize(self.raw("hermes-review-loop:review-loop"))["skill"],
                         "diaktoros:review-loop")
        for kept in ("diaktoros:review-loop", "my-skill", ""):
            self.assertEqual(config.normalize(self.raw(kept))["skill"], kept)


class Manifest(unittest.TestCase):
    def test_the_manifest_names_diaktoros(self):
        text = (ROOT / "plugin.yaml").read_text()
        self.assertIn("\nname: diaktoros\n", "\n" + text)
        self.assertIn("homepage: https://github.com/coe0718/diaktoros\n", text)
        self.assertIn("bounded, autonomous software maintenance for Hermes", text)


if __name__ == "__main__":
    unittest.main()
