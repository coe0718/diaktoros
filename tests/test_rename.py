"""The rename to Diaktoros (#425, stage 1): new names everywhere a person sees one, old ones still read.

`hermes diaktoros` and its short name `hermes dk`; the old `hermes review-loop` alias went in
v0.6.0, the release it named (#1089). What the loop wrote under the old name stays readable: an old footer is still stripped from review history, an old gate shim is
still ours (stale, so rewritten, never refused as foreign), and a loop naming the old qualified
skill is read as the new one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import os
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import attribution, cli, config, gate_shims  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402

OLD_FOOTER = ("\n\n---\n<sub>🤖 Automated by [hermes-review-loop]"
              "(https://github.com/coe0718/hermes-review-loop) · reviewer seat · head `abc1234`</sub>")


class Ctx:
    def __init__(self):
        self.commands: dict = {}

    def register_cli_command(self, name, summary, setup, **kwargs):
        self.commands[name] = setup


class Commands(unittest.TestCase):
    def setUp(self):
        saved = cli_common._PARSER
        self.addCleanup(setattr, cli_common, "_PARSER", saved)
        cli_common._PARSER = None
        self.ctx = Ctx()
        cli.register_cli(self.ctx, settings={})

    def test_two_names_with_the_full_one_registered_last(self):
        self.assertEqual(list(self.ctx.commands), ["dk", "diaktoros"])

    def test_the_old_name_is_gone_in_the_release_it_named(self):
        """#1089: the alias promised "until v0.6.0" is not registered, and the docs say so."""
        self.assertNotIn("review-loop", self.ctx.commands)
        self.assertFalse(hasattr(cli, "RENAMED_NOTE"))
        for doc in (ROOT / "README.md", ROOT / "docs" / "operations.md"):
            text = " ".join(doc.read_text(encoding="utf-8").split())
            self.assertIn("`hermes review-loop` was removed in v0.6.0", text, doc)
            self.assertNotIn("as an alias until", text, doc)

    def test_the_plugin_entry_docstring_names_only_registered_commands(self):
        """#2343: the plugin entry point's docstring names no command register_cli dropped."""
        doc = ast.get_docstring(ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8")))
        named = re.findall(r"``hermes ([\w-]+)``", doc)
        self.assertIn("diaktoros", named)
        for name in named:
            self.assertIn(name, self.ctx.commands, name)


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
        self.assertIn("bounded autonomous software maintenance for Hermes", text)

    def test_the_manifest_and_the_catalog_open_with_the_same_tagline(self):
        # The two copies drifted apart once (the catalog card kept the v0.1.0 wording); one
        # sentence opens both, and each keeps its own disclosure after it.
        # Read without PyYAML (not every CI interpreter has it): the manifest's folded block and
        # the catalog's one quoted line.
        lines = (ROOT / "plugin.yaml").read_text().splitlines()
        start = lines.index("description: >-") + 1
        block = []
        for line in lines[start:]:
            if not line.startswith("  "):
                break
            block.append(line.strip())
        manifest = " ".join(" ".join(block).split())
        catalog = re.search(r'^description: "(.*)"$', (ROOT / "catalog" / "diaktoros.yaml").read_text(),
                            re.M).group(1)
        tagline = catalog.split(" Disclosure —")[0]
        self.assertTrue(tagline.startswith("Bounded autonomous software maintenance for Hermes"))
        self.assertIn("triaged", tagline)
        self.assertIn("the loop never merges.", tagline)
        self.assertEqual(manifest.split(" Disclosure —")[0], "Diaktoros — " + tagline[0].lower() + tagline[1:])


if __name__ == "__main__":
    unittest.main()
