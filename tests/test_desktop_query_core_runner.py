"""Parser/launch guard fixtures, not evidence of component execution."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: isolate operator state
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import run_desktop_query_core as runner


def tap_fixture():
    names = list(runner.REQUIRED_REAL_TESTS)
    names += [f"synthetic parser case {i}" for i in range(runner.EXPECTED_TESTS - len(names))]
    return "\n".join([
        "TAP version 13",
        *(f"ok {i} - {name}" for i, name in enumerate(names, 1)),
        f"1..{runner.EXPECTED_TESTS}",
        f"# tests {runner.EXPECTED_TESTS}", "# suites 0", f"# pass {runner.EXPECTED_TESTS}",
        "# fail 0", "# cancelled 0", "# skipped 0", "# todo 0", "",
    ])


class TapContract(unittest.TestCase):
    def test_complete_explicit_parser_fixture_is_accepted(self):
        runner.validate_tap(tap_fixture())

    def test_absent_real_block_is_rejected_even_with_matching_total(self):
        with self.assertRaisesRegex(ValueError, "missing or duplicated"):
            runner.validate_tap(tap_fixture().replace(runner.REQUIRED_REAL_TESTS[0], "unrelated fixture"))

    def test_skipped_or_todo_real_test_is_rejected(self):
        for directive in ("SKIP unavailable", "TODO later"):
            with self.subTest(directive=directive), self.assertRaisesRegex(ValueError, "skipped or TODO"):
                runner.validate_tap(tap_fixture().replace(runner.REQUIRED_REAL_TESTS[0],
                                                        runner.REQUIRED_REAL_TESTS[0] + " # " + directive))

    def test_count_plan_and_summary_mismatches_are_rejected(self):
        output = tap_fixture()
        for malformed in (
            output.replace(f"ok {runner.EXPECTED_TESTS} -", "ok 999 -"),
            output.replace(f"1..{runner.EXPECTED_TESTS}", "1..1"),
            output.replace(f"# pass {runner.EXPECTED_TESTS}", "# pass 1"),
            output.replace("# skipped 0", "# skipped 1"),
            output.replace("# fail 0", "# fail 1"),
            output.replace("# cancelled 0", "# cancelled 1"),
            output.replace("# todo 0", "# todo 1"),
            output.replace("# suites 0", "# suites 1"),
            output.replace("# fail 0", ""),
            output + "# pass 202\n",
        ):
            with self.subTest(output=malformed[-120:]), self.assertRaises(ValueError):
                runner.validate_tap(malformed)

    def test_no_output_version_stub_and_failure_are_rejected(self):
        for output in ("", "v24.0.0\n", tap_fixture().replace("ok 1 -", "not ok 1 -")):
            with self.subTest(output=output[:40]), self.assertRaises(ValueError):
                runner.validate_tap(output)

    def test_duplicate_required_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing or duplicated"):
            runner.validate_tap(tap_fixture().replace(runner.REQUIRED_REAL_TESTS[1], runner.REQUIRED_REAL_TESTS[0]))


class LaunchContract(unittest.TestCase):
    def test_missing_package_fails_without_launching_node(self):
        with mock.patch.object(runner, "query_core_entry", side_effect=FileNotFoundError("npm ci required")), \
             mock.patch.object(runner.subprocess, "run") as run, \
             mock.patch("sys.stderr"):
            self.assertEqual(runner.main(), 1)
        run.assert_not_called()

    def test_launcher_uses_explicit_entry_and_drops_ambient_overrides(self):
        entry = Path("/explicit/query-core/index.js")
        with mock.patch.object(runner, "query_core_entry", return_value=entry), \
             mock.patch.dict(runner.os.environ, {"DIAKTOROS_QUERY_CORE": "/wrong",
                             "DIAKTOROS_PLUGIN_SOURCE": "/wrong-plugin", "NODE_OPTIONS": "--test-skip-pattern=.*",
                             "NODE_PATH": "/ambient"}), \
             mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, tap_fixture(), "")) as run, \
             mock.patch("sys.stdout"):
            self.assertEqual(runner.main(), 0)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["DIAKTOROS_QUERY_CORE"], entry.as_uri())
        for name in ("NODE_OPTIONS", "NODE_PATH", "DIAKTOROS_PLUGIN_SOURCE"):
            self.assertNotIn(name, kwargs["env"])
        self.assertEqual(run.call_args.args[0][-1], str(runner.ROOT / "tests/desktop_behavior.mjs"))
        self.assertIn("--test-reporter=tap", run.call_args.args[0])

    def test_nonzero_node_exit_rejected_even_with_passing_tap(self):
        with mock.patch.object(runner, "query_core_entry", return_value=Path("/explicit/index.js")), \
             mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, tap_fixture(), "")), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(runner.main(), 1)

    def test_wrong_installed_package_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            (package / "node_modules/@tanstack/query-core").mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"dependencies": {"@tanstack/query-core": runner.QUERY_CORE_VERSION}}))
            (package / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/@tanstack/query-core": {"version": runner.QUERY_CORE_VERSION}}}))
            (package / "node_modules/@tanstack/query-core/package.json").write_text('{"version":"0.0.0"}')
            with mock.patch.object(runner, "PACKAGE", package), self.assertRaisesRegex(ValueError, "must all pin"):
                runner.query_core_entry()


if __name__ == "__main__":
    unittest.main()
