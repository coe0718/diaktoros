"""The CI shards (#850) run every test module exactly once: none dropped, none run twice."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shard


class Shards(unittest.TestCase):
    def test_every_module_is_in_exactly_one_shard(self):
        discovered = set(shard.modules())
        self.assertIn("test_shard", discovered)
        for count in (1, 2, 3, 4):
            with self.subTest(count=count):
                split = shard.shards(count)
                flat = [name for part in split for name in part]
                self.assertEqual(len(flat), len(set(flat)), "a module is in two shards")
                self.assertEqual(set(flat), discovered, "a module is in no shard")

    def test_a_new_file_without_a_weight_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            weights = Path(tmp) / "shard_weights.json"
            weights.write_text(json.dumps({"test_a": 9, "test_b": 1}))
            with mock.patch.object(shard, "modules",
                                   return_value=["test_a", "test_b", "test_new"]), \
                 mock.patch.object(shard, "WEIGHTS", weights):
                split = shard.shards(2)
        self.assertEqual(sorted(n for part in split for n in part), ["test_a", "test_b", "test_new"])

    def test_the_split_is_balanced_and_stable(self):
        with mock.patch.object(shard, "weights",
                               return_value={"test_a": 8, "test_b": 5, "test_c": 4, "test_d": 3}):
            self.assertEqual(shard.shards(2), [["test_a", "test_d"], ["test_b", "test_c"]])

    def test_the_cli_refuses_a_shard_out_of_range(self):
        self.assertEqual(shard.main(["3", "4"]), 2)
        self.assertEqual(shard.main(["3", "0"]), 2)


class Verticals(unittest.TestCase):
    """The verticals job runs in parts (#850): each vertical module is in exactly one part."""

    def parts(self):
        text = (shard.TESTS.parent / ".github" / "workflows" / "ci.yml").read_text()
        job = text.split("\n  verticals:\n", 1)[1].split("\n  # ", 1)[0]
        return [block.split() for block in re.findall(r"modules: >-\n((?:\s{14}\S+\n)+)", job)]

    def test_every_vertical_module_runs_in_exactly_one_part(self):
        parts = self.parts()
        self.assertGreaterEqual(len(parts), 2)
        listed = [name for part in parts for name in part]
        self.assertEqual(len(listed), len(set(listed)), "a module is in two parts")
        files = {f"tests.{p.stem}" for p in shard.TESTS.glob("test_*_vertical.py")}
        self.assertTrue(files)
        self.assertLessEqual(files, set(listed), "a *_vertical module is in no part")


if __name__ == "__main__":
    unittest.main()
