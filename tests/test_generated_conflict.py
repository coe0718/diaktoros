"""#551: a conflict confined to registered generated files is settled by the host, no fixer turn.

Driven through the enforcing entry point, the production worker (``_run_production`` ->
``_prepare_conflict_launch``): the loop's generator runs in a (faked) sandbox on the merged tree,
and its result is pushed through ``safe_push.push`` as the merge. Anything else falls back to the
normal resolving turn.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_conflict_turn as tct  # noqa: E402
import test_seat_models as sm  # noqa: E402
from diaktoros import config, contained, generated, safe_push, show, trusted_turn  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

GEN = [{"paths": ["docs/commands.md"], "command": "regen --write"}]
CONFLICT = b"# Commands\n<<<<<<< ours\nflag a\n=======\nflag b\n>>>>>>> theirs\n"
PROSE = (b"<<<<<<< ours\nhand written a\n=======\nhand written b\n>>>>>>> theirs\n"
         b"<!-- flags:x -->\n<!-- /flags -->\n")
REGENERATED = b"# Commands\nflag a\nflag b\n"


def merged_of(files: dict[str, bytes], conflicted: list[str]) -> dict:
    buf = io.BytesIO()
    entries = []
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(f"merge/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            entries.append((path, oid, len(data), False))
    return {**tct.MERGED, "conflicted": conflicted, "entries": entries, "skipped": [],
            "archive": buf.getvalue()}


class Settled(tct.Worker):
    def setUp(self):
        super().setUp()
        self.loop["generated"] = GEN
        self.pushes: list[dict] = []
        self.sandbox_runs = 0

    def fake_sandbox(self, generator):
        """contained.run that really runs the wrapper with ``generator(checkout)`` as the command."""
        def run(**kw):
            self.sandbox_runs += 1
            checkout = Path(kw["checkout"])
            entry = kw["entry"]
            generator(checkout)
            return subprocess.run(["/bin/sh", "-c", generated.WRAPPER, "sh", "true", *entry[5:]],
                                  cwd=checkout, capture_output=True, text=True)
        return run

    def settle(self, merged, generator=lambda d: (d / "docs/commands.md").write_bytes(REGENERATED),
               sandbox=None, push_error=None):
        def push(loop, **kw):
            self.pushes.append(kw)
            if push_error:
                raise push_error
            return {"new_head": "d" * 40}
        with mock.patch.object(trusted_turn, "_safe_code_snapshot",
                               side_effect=lambda src, dst: dst.mkdir()), \
             mock.patch.object(contained, "run", side_effect=sandbox or self.fake_sandbox(generator)), \
             mock.patch.object(safe_push, "push", side_effect=push):
            return self.conflict_run(merged=merged)

    def test_a_conflict_confined_to_the_generated_file_is_pushed_regenerated_with_no_turn(self):
        seen, row, reviews, _ = self.settle(merged_of({"docs/commands.md": CONFLICT},
                                                      ["docs/commands.md"]))
        self.assertEqual(seen, {}, "no fixer turn launched")
        self.assertEqual(row[0], "succeeded")
        (push,) = self.pushes
        self.assertEqual(push["merge"], {"base_ref": "main", "base_sha": tct.BASE_SHA,
                                         "tree": tct.TREE})
        (entry,) = push["manifest"]["files"]
        self.assertEqual(entry["path"], "docs/commands.md")
        import base64
        self.assertEqual(base64.b64decode(entry["content_b64"]), REGENERATED)
        self.assertEqual(push["manifest"]["base_head"], sm.HEAD)

    def test_removing_the_regeneration_turns_it_red(self):
        with mock.patch.object(generated, "regenerate", return_value=None):
            seen, row, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT},
                                                  ["docs/commands.md"]))
        self.assertIn("prompt", seen, "without regeneration the fixer turn runs")
        self.assertEqual(self.pushes, [])

    def test_a_mixed_conflict_still_gets_a_turn_and_runs_no_generator(self):
        seen, row, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT, "a.py": CONFLICT},
                                              ["a.py", "docs/commands.md"]))
        self.assertIn("prompt", seen)
        self.assertEqual((self.sandbox_runs, self.pushes), (0, []))

    def test_the_feature_is_off_by_default(self):
        self.loop["generated"] = []
        seen, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT}, ["docs/commands.md"]))
        self.assertIn("prompt", seen)
        self.assertEqual(self.sandbox_runs, 0)

    def test_a_conflict_in_hand_written_prose_still_gets_a_turn(self):
        # The generator rewrites only its blocks, so the prose conflict's markers survive.
        seen, *_ = self.settle(merged_of({"docs/commands.md": PROSE}, ["docs/commands.md"]),
                               generator=lambda d: None)
        self.assertIn("prompt", seen)
        self.assertEqual(self.pushes, [])

    def test_a_file_written_outside_the_registered_paths_is_not_pushed(self):
        def generator(d):
            (d / "docs/commands.md").write_bytes(REGENERATED)
            (d / "evil.py").write_text("print('x')\n")
        self.settle(merged_of({"docs/commands.md": CONFLICT}, ["docs/commands.md"]),
                    generator=generator)
        (push,) = self.pushes
        self.assertEqual([f["path"] for f in push["manifest"]["files"]], ["docs/commands.md"])

    def test_a_generator_that_fails_or_times_out_falls_back_to_the_turn(self):
        def failing(d):
            raise AssertionError("unused")
        for label, sandbox in (
                ("exit 1", lambda **kw: subprocess.CompletedProcess([], 1, "", "boom")),
                ("timeout", mock.Mock(side_effect=subprocess.TimeoutExpired("sh", 1))),
                ("no output", lambda **kw: subprocess.CompletedProcess([], 0, "", "")),
                ("oversize", lambda **kw: subprocess.CompletedProcess(
                    [], 0, "docs/commands.md "
                    + __import__("base64").b64encode(b"x" * (generated.FILE_MAX + 1)).decode()
                    + "\n", ""))):
            with self.subTest(label):
                self.pushes.clear()
                seen, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT},
                                                 ["docs/commands.md"]), sandbox=sandbox)
                self.assertIn("prompt", seen)
                self.assertEqual(self.pushes, [])

    def test_a_duplicate_output_record_falls_back_to_the_turn(self):
        b64 = __import__("base64").b64encode
        for label, second in (("different", REGENERATED + b"extra\n"), ("identical", REGENERATED)):
            with self.subTest(label):
                self.pushes.clear()
                out = ("docs/commands.md " + b64(REGENERATED).decode() + "\n"
                       "docs/commands.md " + b64(second).decode() + "\n")
                seen, *_ = self.settle(
                    merged_of({"docs/commands.md": CONFLICT}, ["docs/commands.md"]),
                    sandbox=lambda **kw: subprocess.CompletedProcess([], 0, out, ""))
                self.assertIn("prompt", seen)
                self.assertEqual(self.pushes, [])

    def test_parse_rejects_a_duplicate_record(self):
        with self.assertRaisesRegex(ValueError, "duplicate output for a"):
            generated._parse("a eA==\na eA==\n", ["a"])

    def test_a_push_refused_before_it_wrote_falls_back_to_the_turn(self):
        from diaktoros import broker
        seen, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT}, ["docs/commands.md"]),
                               push_error=broker.BrokerDenied("PR branch moved"))
        self.assertIn("prompt", seen)

    def test_a_push_that_may_have_written_is_quarantined_not_retried(self):
        seen, row, *_ = self.settle(merged_of({"docs/commands.md": CONFLICT}, ["docs/commands.md"]),
                                    push_error=safe_push.PushFailure("unknown"))
        self.assertEqual(seen, {})
        self.assertEqual(row[0], "uncertain")


class Setting(tct.unittest.TestCase):
    RAW = {"id": "w", "repo": "a/b", "tokens": {}, "read_token": "r", "fixers": ["f"],
           "reviewers": ["r"]}

    def test_validation(self):
        self.assertEqual(config.check_generated(None, "x"), [])
        for bad in ("docs", [{"paths": [], "command": "c"}], [{"paths": ["../a"], "command": "c"}],
                    [{"paths": ["/a"], "command": "c"}], [{"paths": ["a/./b"], "command": "c"}],
                    [{"paths": ["a"], "command": "c\nd"}], [{"paths": ["a"], "command": ""}],
                    [{"paths": ["a"], "command": "c"}, {"paths": ["a"], "command": "d"}],
                    [{"paths": ["a"], "command": "c", "x": 1}], [{"paths": ["a"]}]):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.check_generated(bad, "x")
        self.assertEqual(config.check_generated(GEN, "x"), GEN)
        self.assertEqual(config.DEFAULTS["generated"], [])

    def test_form_cli_and_show(self):
        text = "docs/commands.md=regen --write"
        self.assertEqual(config.parse_generated(config.split_generated(text), "f"), GEN)
        self.assertIn("generated", settings_form.SETTINGS_SCHEMA)
        self.assertNotIn("generated", settings_form.apply_settings(dict(self.RAW), {}))
        self.assertEqual(settings_form.apply_settings(dict(self.RAW), {"generated": text})["generated"],
                         GEN)
        with self.assertRaises(config.ConfigError):
            settings_form.apply_settings(dict(self.RAW), {"generated": "no-equals"})
        self.assertEqual(config.generated_text(GEN), text)
        self.assertEqual(config.generated({"generated": GEN}), GEN)


if __name__ == "__main__":
    tct.unittest.main()
