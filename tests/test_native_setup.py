"""Native platform surfaces, with disposable paths and no live service/config access."""
import _home_guard  # noqa: F401
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from diaktoros import contained, doctor, runtime_detect, selftest
from diaktoros.cli import setup_command


class NativeSetup(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'DIAKTOROS_NATIVE_MACOS': '1'}))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        platform = patch.object(sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.settings = {key: str(self.root / key) for key in runtime_detect.HOST_KEYS}
        for value in self.settings.values():
            Path(value).mkdir()
        for key, names in (("venv", ("python", "hermes")), ("rust", ("cargo", "rustc"))):
            for name in names:
                file = Path(self.settings[key]) / "bin" / name
                file.parent.mkdir(exist_ok=True)
                file.write_text("#!/bin/sh\nexit 0\n")
                file.chmod(0o700)
        Path(self.settings["source"], "run_agent.py").touch()
        Path(self.settings["source"], ".git").mkdir()

    def test_framework_alias_uses_real_generation_not_homebrew_prefix(self):
        runtime = Path(self.settings["runtime"])
        binary = runtime / "bin" / "python3"
        binary.parent.mkdir()
        binary.touch()
        alias = self.root / "opt" / "python"
        alias.parent.mkdir()
        alias.symlink_to(runtime, target_is_directory=True)
        link = Path(self.settings["venv"]) / "bin" / "python"
        link.unlink()
        link.symlink_to(alias / "bin" / "python3")
        self.assertEqual(runtime_detect.runtime_for(Path(self.settings["venv"])), runtime)
        self.assertEqual(selftest._interpreter_outside(link, runtime), "")
        self.assertTrue(selftest._interpreter_outside(link, Path(self.settings["rust"])))

    def test_framework_python_selects_only_an_executable_inside_its_runtime(self):
        venv = Path(self.settings["venv"])
        runtime = Path(self.settings["runtime"])
        (runtime / "bin").mkdir()
        (runtime / "bin/python").write_text("launcher")
        link = venv / "bin/python"
        link.unlink()
        link.symlink_to(runtime / "bin/python")
        app = runtime / "Resources/Python.app/Contents/MacOS/Python"
        app.parent.mkdir(parents=True)
        app.touch()
        app.chmod(0o700)
        self.assertEqual(runtime_detect.native_python(venv), app)
        app.unlink()
        app.symlink_to(Path(self.settings["rust"]) / "bin/cargo")
        with self.assertRaises(ValueError):
            runtime_detect.native_python(venv)

    def test_broad_home_state_and_alias_roots_are_rejected(self):
        home = self.root / "operator"
        state = home / ".hermes"
        state.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(state, target_is_directory=True)
        with patch.object(runtime_detect, "_homes", return_value={home}), \
                patch.object(runtime_detect.config, "home", return_value=state):
            for value in ("/", "/usr", "/opt/homebrew", str(home), str(state), str(alias)):
                with self.subTest(value=value):
                    self.assertTrue(runtime_detect.native_root_problem(value))
            dedicated = state / "node"
            dedicated.mkdir()
            self.assertEqual(runtime_detect.native_root_problem(str(dedicated)), "")
        self.assertTrue(runtime_detect.native_root_problem("relative"))
        self.assertTrue(runtime_detect.native_root_problem(str(self.root) + "\n"))

    def test_toolchain_binaries_cannot_escape_root_or_be_nonexecutable(self):
        self.assertEqual(runtime_detect.native_problems(self.settings), {})
        cargo = Path(self.settings["rust"]) / "bin" / "cargo"
        cargo.chmod(0o600)
        self.assertIn("executable", runtime_detect.native_problems(self.settings)["rust"])
        cargo.unlink()
        cargo.symlink_to(Path(self.settings["venv"]) / "bin" / "hermes")
        self.assertIn("leaves", runtime_detect.native_problems(self.settings)["rust"])

    def test_native_node_follows_absolute_internal_links_but_not_prefix_escape(self):
        root = self.root / "node-generation"
        (root / "bin").mkdir(parents=True)
        binary = root / "node-real"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o700)
        link = root / "bin" / "node"
        link.symlink_to(binary)
        self.assertEqual(runtime_detect.node_problem(str(root)), "")
        link.unlink()
        link.symlink_to(Path(self.settings["venv"]) / "bin" / "hermes")
        self.assertIn("inside", runtime_detect.node_problem(str(root)))

    def test_sdk_tools_are_narrow_and_missing_tools_fail(self):
        sdk = self.root / "SDK"
        sdk.mkdir()
        tools = self.root / "Developer" / "usr"
        (tools / "bin").mkdir(parents=True)
        for name in ("clang", "ar"):
            file = tools / "bin" / name
            file.touch()
            file.chmod(0o700)
        results = [subprocess.CompletedProcess([], 0, str(sdk) + "\n", ""),
                   subprocess.CompletedProcess([], 0, str(tools / "bin" / "clang") + "\n", "")]
        with patch("subprocess.run", side_effect=results):
            found, why = runtime_detect.native_tools()
        self.assertFalse(why)
        self.assertEqual(found, {"sdk": sdk, "developer_tools": tools})
        with patch("subprocess.run", side_effect=OSError):
            self.assertTrue(runtime_detect.native_tools()[1])

    def test_setup_stops_before_routes_when_native_runtime_fails(self):
        args = argparse.Namespace(yes=True, repo="acme/test", id="test", dry_run=False)
        with patch.object(setup_command, "_setup_runtime", return_value=False), \
                patch.object(setup_command, "_setup_loop") as install, \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(setup_command.cmd_setup(args), 1)
        install.assert_not_called()

    def test_setup_missing_prerequisite_never_writes(self):
        with patch.object(contained, "unavailable", return_value="no Seatbelt"), \
                patch.object(runtime_detect, "native_tools", return_value=({}, "missing SDK")), \
                patch.object(runtime_detect, "write") as write, \
                patch("sys.stdout", new=io.StringIO()):
            self.assertFalse(setup_command._setup_runtime(argparse.Namespace(), False))
        write.assert_not_called()

    def test_doctor_labels_intel_unverified_and_prerequisite_failure(self):
        with patch.object(contained, "unavailable", return_value="no Seatbelt"), \
                patch.object(runtime_detect, "native_tools", return_value=({}, "missing SDK")), \
                patch("platform.machine", return_value="x86_64"):
            checks = doctor.check_native_platform()
        self.assertEqual([row.status for row in checks], [doctor.MISMATCH, doctor.UNKNOWN])

    def test_native_cleanup_pending_and_uncertain_never_report_verified(self):
        from diaktoros import native_cleanup
        for status, expected in ((None, None),
                                 ({"paused": True, "uncertain_notices": 0, "detail": "retained"}, doctor.MISMATCH),
                                 ({"paused": False, "uncertain_notices": 1, "detail": "uncertain"}, doctor.UNKNOWN)):
            with self.subTest(status=status), patch.object(native_cleanup, "ledger_status", return_value=status):
                check = doctor.check_native_cleanup()
                self.assertEqual(check.status if check else None, expected)

    def test_doctor_native_storage_uses_disk_not_memory(self):
        with patch.object(doctor, "_host_memory", side_effect=AssertionError("RAM queried")), \
                patch.object(doctor.config, "worst_case_turns", return_value=1):
            check = doctor.check_sandbox_caps({"state_dir": str(self.root)})
        self.assertIn("APFS", check.detail)
        self.assertIn("disk", check.detail)

    def test_native_selftest_prerequisites_fail_not_skip(self):
        report = selftest.Report(io.StringIO())
        with patch.object(contained, "unavailable", return_value="no Seatbelt"), \
                patch.object(runtime_detect, "native_tools", return_value=({}, "missing SDK")), \
                patch.object(contained, "run") as run:
            selftest.check_bwrap(report, {}, None, self.root / "runtime.json")
        self.assertEqual(report.results[0][2], selftest.FAIL)
        run.assert_not_called()

    def test_native_selftest_dispatches_real_run_contract_and_rejects_escape(self):
        from diaktoros import trusted_turn
        loop = {"id": "isolated", "state_dir": str(self.root / "state"), "tokens": {}}
        facts = {"readable": [], "network": False, "dns": False, "cargo": True,
                 "hermes": True, "env": [], "process_denied": [False, True]}
        report = selftest.Report(io.StringIO())
        with patch.object(contained, "unavailable", return_value=""), \
                patch.object(runtime_detect, "native_tools", return_value=({"sdk": self.root}, "")), \
                patch.object(trusted_turn, "_safe_code_snapshot", side_effect=lambda s, d: d.mkdir()), \
                patch.object(selftest, "host_secret_paths", return_value=[]), \
                patch.object(contained, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(facts), "")) as run:
            selftest.check_bwrap(report, loop, self.settings, self.root / "runtime.json")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["entry"][0], str(Path(self.settings["venv"]) / "bin/python"))
        self.assertTrue(any(row[1] == "sandbox:process" and row[2] == selftest.FAIL for row in report.results))
        self.assertFalse(any(row[1].startswith("bwrap:") for row in report.results))


@unittest.skipUnless(sys.platform == "darwin", "requires actual macOS containment")
class NativeSelftestBoundary(unittest.TestCase):
    def test_containment_probe_uses_real_native_backend_without_credentials(self):
        self.enterContext(patch.dict(os.environ, {'DIAKTOROS_NATIVE_MACOS': '1'}))
        from diaktoros import native_backend, trusted_turn
        self.assertEqual(native_backend.unavailable(), "")
        self.assertEqual(runtime_detect.native_tools()[1], "")
        root = Path(tempfile.mkdtemp(prefix="dk-selftest-")).resolve()
        venv, rust = root / "venv", root / "rust"
        for path in (venv / "bin", rust / "bin"):
            path.mkdir(parents=True)
        python = Path(sys.executable).resolve()
        (venv / "bin/python").symlink_to(python)
        # Only the boundary is real: these offline fixtures are not a Hermes/Rust acceptance.
        for file in (venv / "bin/hermes", rust / "bin/cargo"):
            file.write_text("#!/bin/sh\necho fixture\n")
            file.chmod(0o700)
        settings = {"source": str(root), "venv": str(venv),
                    "runtime": str(python.parent.parent), "rust": str(rust)}
        loop = {"id": "offline", "state_dir": str(root / "state"), "tokens": {}}
        output = io.StringIO()
        report = selftest.Report(output)
        create = native_backend.workspace
        with patch.object(native_backend, "workspace", side_effect=lambda: create(pool=root / "pool", size_mib=64)), \
                patch.object(trusted_turn, "_safe_code_snapshot", side_effect=lambda s, d: d.mkdir()), \
                patch.object(selftest, "host_secret_paths", return_value=[]):
            selftest.check_bwrap(report, loop, settings, root / "runtime.json")
        failures = [row for row in report.results if row[2] != selftest.PASS]
        self.assertEqual(failures, [], f"{output.getvalue()}\nretained evidence: {root}")
        self.assertTrue(any(row[1] == "sandbox:process" for row in report.results))
        from diaktoros import native_cleanup
        self.assertEqual(native_cleanup.Manager(root / "pool", create=False).status()["reserved_bytes"], 0)
        import shutil
        shutil.rmtree(root)  # Only after verified backend completion and an empty pool.



if __name__ == "__main__":
    unittest.main()
