"""Host Node detection and its doctor line (#685)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import errno
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import contained, doctor, runtime_detect  # noqa: E402
from test_sandbox_limits import bwrap_works  # noqa: E402


class NodeRuntime(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"HERMES_REAL_HOME": str(self.home), "PATH": "/nonexistent"})
        env.start()
        self.addCleanup(env.stop)

    def install(self, root: Path, version: str = "v22.3.0") -> Path:
        (root / "bin").mkdir(parents=True)
        node = root / "bin" / "node"
        node.write_text(f"#!/bin/sh\necho {version}\n")
        node.chmod(0o755)
        return root

    def test_finds_hermes_home_node_behind_a_user_local_link(self):
        root = self.install(self.home / ".hermes" / "node")
        link = self.home / ".local" / "bin"
        link.mkdir(parents=True)
        (link / "node").symlink_to(root / "bin" / "node")
        self.assertEqual(runtime_detect.node_root(), root)
        self.assertEqual(runtime_detect.node_version(), (root, "v22.3.0"))

    def test_home_or_credential_store_is_never_a_node_root(self):
        for rel in ("bin", ".local/bin", ".hermes/bin", ".config/bin"):
            with self.subTest(rel=rel):
                node = self.home / rel / "node"
                node.parent.mkdir(parents=True, exist_ok=True)
                node.write_text("#!/bin/sh\necho v22.3.0\n")
                node.chmod(0o755)
                with mock.patch.dict(os.environ, {"PATH": str(node.parent)}):
                    self.assertIsNone(runtime_detect.node_root())
                    self.assertIsNone(runtime_detect.node_version()[0])
                node.unlink()

    def test_node_tree_inside_a_dot_directory_is_still_accepted(self):
        root = self.install(self.home / ".local" / "share" / "node-v22")
        with mock.patch.dict(os.environ, {"PATH": str(root / "bin")}):
            self.assertEqual(runtime_detect.node_root(), root)

    def test_unsafe_candidate_does_not_hide_a_later_real_install(self):
        bad = self.home / "bin"
        bad.mkdir()
        (bad / "node").write_text("#!/bin/sh\n")
        (bad / "node").chmod(0o755)
        root = self.install(self.home / "opt" / "node")
        with mock.patch.dict(os.environ, {"PATH": f"{bad}:{root / 'bin'}"}):
            self.assertEqual(runtime_detect.node_root(), root)

    def test_no_node_is_a_reported_skip_not_a_failure(self):
        self.assertIsNone(runtime_detect.node_root())
        check = doctor.check_node()
        self.assertEqual(check.name, "runtime:node")
        self.assertEqual(check.status, doctor.SKIPPED)
        self.assertFalse(check.failed)

    def test_doctor_reports_version_through_the_runtime_checks(self):
        root = self.install(self.home / ".hermes" / "node", "v20.1.0")
        check = doctor.check_node({"node": str(root)})
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("v20.1.0", check.detail)
        self.assertIn(str(root), check.detail)
        self.assertIn("mounted at /opt/node", check.detail)

    def test_a_host_node_the_runtime_file_does_not_record_is_a_warning(self):
        # #1149: doctor used to look in the operator's shell and say "mounted" while the worker,
        # in its own clean environment, found nothing. Seats get only the recorded Node.
        root = self.install(self.home / ".hermes" / "node")
        for settings in ({}, {"source": "/s"}):
            with self.subTest(settings=settings):
                check = doctor.check_node(settings)
                self.assertEqual(check.status, doctor.UNKNOWN)
                self.assertFalse(check.failed)
                self.assertIn(f"{root} is not in the runtime file", check.detail)
                self.assertIn("hermes dk setup", check.fix)
        # #2478 review 2, F7: recorded as none, it is still a host Node seats don't get.
        check = doctor.check_node({"node": ""})
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertFalse(check.failed)
        self.assertIn(f"{root} is not in the runtime file (it records no Node)", check.detail)
        self.assertIn("hermes dk setup", check.fix)
        self.assertEqual(self.selftest_node(node="")[0], "warn")

    def test_a_recorded_node_that_is_gone_is_a_warning_and_mounts_nothing(self):
        gone = str(self.home / "node-v20")
        check = doctor.check_node({"node": gone})
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("seats get no Node", check.detail)
        with mock.patch.object(runtime_detect.util, "log") as log:
            self.assertIsNone(runtime_detect.recorded_node({"node": gone}))
        self.assertIn("re-run `hermes dk setup`", log.call_args[0][0])

    def test_the_worker_mounts_only_the_recorded_node(self):
        root = self.install(self.home / "opt" / "node")
        self.assertEqual(runtime_detect.recorded_node({"node": str(root)}), root)
        self.assertIsNone(runtime_detect.recorded_node({"node": ""}))
        self.assertIsNone(runtime_detect.recorded_node({}))
        self.install(self.home)                                # ~/bin/node: the home is no root
        with mock.patch.object(runtime_detect.util, "log"):
            self.assertIsNone(runtime_detect.recorded_node({"node": str(self.home)}))
        with mock.patch.object(runtime_detect, "_NODE_VISIBLE", (root,)):
            self.assertIsNone(runtime_detect.recorded_node({"node": str(root)}),
                              "a /usr install is already visible: nothing to mount")

    def test_a_link_is_checked_and_mounted_as_the_directory_it_names(self):
        # #2478 review F1: a recorded alias of the home (or of a credential store) with a bin/node
        # in it passed the literal-path check and was bound whole. The check and the mount are
        # the canonical directory's.
        self.install(self.home)                                  # ~/bin/node
        self.install(self.home / ".ssh")                         # ~/.ssh/bin/node
        links = self.home / "links"
        links.mkdir()
        for name, target in (("home", self.home), ("ssh", self.home / ".ssh")):
            with self.subTest(name):
                alias = links / name
                alias.symlink_to(target)
                self.assertIn("home directory or credential store", runtime_detect.node_problem(str(alias)))
                with mock.patch.object(runtime_detect.util, "log"):
                    self.assertIsNone(runtime_detect.recorded_node({"node": str(alias)}))
                self.assertEqual(doctor.check_node({"node": str(alias)}).status, doctor.UNKNOWN)
        real = self.install(self.home / "opt" / "node-v22")
        (links / "node").symlink_to(real)
        self.assertEqual(runtime_detect.recorded_node({"node": str(links / "node")}), real)

    def test_a_bin_node_that_leaves_its_root_is_refused(self):
        elsewhere = self.install(self.home / "elsewhere")
        root = self.home / "opt" / "node"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "node").symlink_to(elsewhere / "bin" / "node")
        self.assertIn("not an executable file inside", runtime_detect.node_problem(str(root)))

    def layouts(self) -> dict[str, Path]:
        """Node roots whose ``bin/node`` is reached each way: the file itself, a relative link
        inside the root, an absolute link to that same file, and a link loop."""
        roots = {}
        for name in ("file", "relative", "absolute", "loop"):
            root = self.home / "opt" / name
            (root / "bin").mkdir(parents=True)
            (root / "lib").mkdir()
            real = root / ("bin" if name == "file" else "lib") / "node"
            real.write_text('#!/bin/sh\ncase "$1" in --version) echo v22.9.9;; esac\n')
            real.chmod(0o755)
            link = root / "bin" / "node"
            if name == "relative":
                link.symlink_to("../lib/node")
            elif name == "absolute":
                link.symlink_to(real)
            elif name == "loop":
                (root / "bin" / "a").symlink_to("node")
                link.symlink_to("a")
            roots[name] = root
        return roots

    def test_a_bin_node_a_seat_cannot_follow_is_refused_everywhere(self):
        # #2478 review 2, F5: an absolute link to a file inside the root passed and ran on the
        # host, but not in the seat, where the root alone is bound at /opt/node: doctor and
        # selftest said "mounted" and the seat had no node. A relative link inside it works.
        roots = self.layouts()
        for name in ("file", "relative"):
            with self.subTest(name):
                self.assertEqual(runtime_detect.node_problem(str(roots[name])), "")
                self.assertEqual(runtime_detect.recorded_node({"node": str(roots[name])}), roots[name])
                self.assertEqual(doctor.check_node({"node": str(roots[name])}).status, doctor.VERIFIED)
        for name in ("absolute", "loop"):
            with self.subTest(name):
                root = str(roots[name])
                self.assertIn("relative links only", runtime_detect.node_problem(root))
                with mock.patch.object(runtime_detect.util, "log") as log:
                    self.assertIsNone(runtime_detect.turn_paths(
                        {"source": "/s", "venv": "/v", "runtime": "/r", "rust": "/x",
                         "node": root})["node"])
                self.assertIn("re-run `hermes dk setup`", log.call_args[0][0])
                self.assertEqual(doctor.check_node({"node": root}).status, doctor.UNKNOWN)
                self.assertEqual(self.selftest_node(node=root)[0], "warn")

    def test_a_link_loop_is_refused_not_raised_on_any_python(self):
        # #2478 review 2, F6: before Python 3.13, resolving a link loop raises RuntimeError, not
        # OSError; it escaped node_problem and failed the turn instead of mounting no Node.
        real = Path.resolve

        def old_python(path, strict=False):
            try:
                return real(path, strict=strict)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise RuntimeError(f"Symlink loop from {str(path)!r}") from None
                raise
        root = str(self.layouts()["loop"])
        with mock.patch.object(Path, "resolve", old_python), \
                mock.patch.object(runtime_detect.util, "log"):
            self.assertIn("relative links only", runtime_detect.node_problem(root))
            self.assertIsNone(runtime_detect.recorded_node({"node": root}))
            self.assertEqual(doctor.check_node({"node": root}).status, doctor.UNKNOWN)
            (self.home / ".hermes").mkdir()
            (self.home / ".hermes" / "node").symlink_to(root)       # detection's first candidate
            self.assertIsNone(runtime_detect.node_root())

    def test_the_worker_knows_the_accounts_home_whatever_its_home_says(self):
        import pwd
        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with mock.patch.dict(os.environ, {"HOME": str(self.home / "hermes")}):
            os.environ.pop("HERMES_REAL_HOME", None)
            self.assertIn(account, runtime_detect._homes())

    def test_only_a_root_whose_bin_is_on_the_sandbox_path_needs_no_mount(self):
        # #2478 review F2: a seat's PATH holds only /usr/bin and /bin, so /usr/local's node (or a
        # /usr/lib tree's) is no command there unless it is mounted at /opt/node.
        for root, mounted in (("/", False), ("/usr", False), ("/usr/local", True),
                              ("/usr/lib/node-v22", True), ("/opt/node-v22", True)):
            with self.subTest(root=root):
                self.assertIs(runtime_detect.node_mounted(Path(root)), mounted)
        local = (Path("/usr/local"), "v22.0.0")
        with mock.patch.object(runtime_detect, "node_version", return_value=local), \
                mock.patch.object(runtime_detect, "_node_install", return_value=Path("/usr/local")):
            self.assertEqual(runtime_detect.node_root(), Path("/usr/local"))
            self.assertEqual(runtime_detect.detect().get("node"), "/usr/local")
            state, detail, _ = runtime_detect.node_report({})
            self.assertEqual(state, "warn", detail)
            self.assertIn("not in the runtime file", detail)
            check = doctor.check_node({})
            self.assertEqual(check.status, doctor.UNKNOWN)

    def test_setup_detects_only_a_node_to_mount(self):
        root = self.install(self.home / ".hermes" / "node")
        self.assertEqual(runtime_detect.detect().get("node"), str(root))

    def system_install(self):
        """A /usr-style install: found on PATH, but a root the sandbox already sees."""
        root = self.install(self.home / "sysroot", "v18.19.0")
        env = mock.patch.dict(os.environ, {"PATH": str(root / "bin")})
        env.start()
        self.addCleanup(env.stop)
        vis = mock.patch.object(runtime_detect, "_NODE_VISIBLE", (root,))
        vis.start()
        self.addCleanup(vis.stop)
        return root

    def test_system_install_is_available_but_not_mounted(self):
        root = self.system_install()
        self.assertIsNone(runtime_detect.node_root())
        self.assertEqual(runtime_detect.node_version(), (root, "v18.19.0"))
        self.assertFalse(runtime_detect.node_mounted(root))

    def test_doctor_reports_system_node_as_verified(self):
        self.system_install()
        check = doctor.check_node()
        self.assertEqual(check.status, doctor.VERIFIED)
        self.assertIn("v18.19.0", check.detail)
        self.assertIn("already visible", check.detail)

    def test_selftest_reports_system_node_as_pass(self):
        self.system_install()
        state, detail = self.selftest_node()
        self.assertEqual(state, "pass")
        self.assertIn("v18.19.0", detail)
        self.assertIn("already visible", detail)

    def test_selftest_checks_the_node_the_runtime_file_records(self):
        # #1149: selftest, like doctor, reads Node the way the worker does.
        root = self.install(self.home / ".hermes" / "node")
        state, detail = self.selftest_node()
        self.assertEqual(state, "warn")
        self.assertIn("not in the runtime file", detail)
        state, detail = self.selftest_node(node=str(root))
        self.assertEqual(state, "pass")
        self.assertIn("mounted at /opt/node", detail)

    def selftest_node(self, **extra) -> tuple[str, str]:
        """``selftest``'s ``runtime:node`` line, from a runtime file of usable paths and ``extra``."""
        import json
        from diaktoros import selftest
        src = self.home / "src"
        (src / ".git").mkdir(parents=True, exist_ok=True)
        (src / "run_agent.py").write_text("")
        venv = self.home / "venv"
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("python", "hermes"):
            (venv / "bin" / name).write_text("")
        rust = self.home / "rust"
        (rust / "bin").mkdir(parents=True, exist_ok=True)
        (rust / "bin" / "cargo").write_text("")
        path = self.home / "runtime.json"
        path.write_text(json.dumps({"source": str(src), "venv": str(venv),
                                    "runtime": "/usr", "rust": str(rust), **extra}))
        path.chmod(0o600)
        report = selftest.Report(out=io.StringIO())
        selftest.check_runtime(report, path)
        node = [r for r in report.results if r[1] == "runtime:node"]
        self.assertEqual(len(node), 1, report.results)
        return node[0][2], node[0][3]

    def test_a_host_that_cannot_start_a_seat_cannot_verify_its_node(self):
        # #2478 review 3, F8: the recorded Node is checked in a seat's sandbox; with no
        # bubblewrap there is none, so it is a warning, never verified from the host.
        root = self.install(self.home / ".hermes" / "node")
        with mock.patch.object(contained, "unavailable", return_value="no bwrap"):
            check = doctor.check_node({"node": str(root)})
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("sandbox could not be started", check.detail)

    def test_a_broken_node_is_not_reported_as_available(self):
        root = self.install(self.home / ".hermes" / "node", "garbage")
        self.assertEqual(runtime_detect.node_version(root)[0], None)


@unittest.skipUnless(bwrap_works(), "unprivileged bubblewrap unavailable")
class NodeInRealSandbox(unittest.TestCase):
    """#685: run through real bubblewrap; the seat finds `node` on PATH only via the mount.

    The install is a stand-in script (no real Node exists where this runs): it proves the
    bind and PATH entry, which is all the launcher controls. A real host Node needs `selftest`.
    """
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for name in ("code", "home", "export"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "q.txt").write_text("q\n")
        self.node = self.root / "node-install"
        (self.node / "bin").mkdir(parents=True)
        script = self.node / "bin" / "node"
        script.write_text('#!/bin/sh\ncase "$1" in --version) echo v22.9.9;; '
                          '--test) echo "ok $2"; echo "# pass 1";; esac\n')
        script.chmod(0o755)

    def run_in_sandbox(self, shell: str, node):
        kw = dict(code=self.root / "code", venv=Path("/usr"), runtime=Path("/usr"),
                  home=self.root / "home", checkout=self.root / "export", rust=Path("/usr"),
                  query=self.root / "q.txt", entry=["/bin/sh", "-c", shell])
        if node is not None:
            kw["node"] = node
        return contained.run(timeout=60, **kw)

    def test_seat_runs_node_version_and_node_test(self):
        done = self.run_in_sandbox("node --version && node --test /work/x.test.js", self.node)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = done.stdout.decode() if isinstance(done.stdout, bytes) else done.stdout
        self.assertIn("v22.9.9", out)
        self.assertIn("ok /work/x.test.js", out)

    def test_the_seat_runs_exactly_the_bin_node_the_check_accepts(self):
        # #2478 review 2, F5: node_problem's verdict is the seat's. Each layout's root is bound
        # at /opt/node as the worker would bind it; the seat runs node only where it passed.
        case = NodeRuntime()
        case.home = self.root
        for name, root in case.layouts().items():
            with self.subTest(name):
                accepted = runtime_detect.node_problem(str(root)) == ""
                done = self.run_in_sandbox("node --version", root)
                self.assertEqual(accepted, done.returncode == 0, (name, done.stderr[-500:]))
                self.assertEqual(accepted, name in ("file", "relative"))

    def test_doctor_and_selftest_run_the_recorded_node_in_a_seat(self):
        # #2478 review 3, F8: a bin/node inside the root that runs on the host can still fail in
        # the seat: here a script whose interpreter is a host path the sandbox lacks (a native
        # binary whose loader or libraries it lacks is the same). doctor and selftest ran it on
        # the host and said "mounted"; they now run it where a turn does.
        interpreter = self.root / "host-only" / "sh"
        interpreter.parent.mkdir()
        shutil.copy2("/bin/sh", interpreter)
        wrapped = self.root / "wrapped-node"
        (wrapped / "bin").mkdir(parents=True)
        (wrapped / "bin" / "node").write_text(f"#!{interpreter}\necho v22.1.0\n")
        (wrapped / "bin" / "node").chmod(0o755)
        self.assertEqual(runtime_detect.node_problem(str(wrapped)), "")      # inside its root
        self.assertEqual(runtime_detect.node_version(wrapped), (wrapped, "v22.1.0"))  # host: fine
        self.assertNotEqual(self.run_in_sandbox("node --version", wrapped).returncode, 0)
        case = NodeRuntime()
        case.home = self.root
        for root, works in ((self.node, True), (wrapped, False)):
            with self.subTest(str(root.name)):
                check = doctor.check_node({"node": str(root)})
                state, detail = case.selftest_node(node=str(root))
                if works:
                    self.assertEqual(check.status, doctor.VERIFIED, check.detail)
                    self.assertIn("v22.9.9", check.detail)
                    self.assertEqual(state, "pass", detail)
                else:
                    self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
                    # The rc is the seat shell's: bash says 126 (bad interpreter), dash 127.
                    self.assertRegex(check.detail, r"failed in a seat's sandbox \(rc 12[67]: ")
                    self.assertIn("seats get no Node", check.detail)
                    self.assertEqual(state, "warn", detail)

    def test_without_the_mount_the_seat_has_no_node(self):
        done = self.run_in_sandbox("node --version", None)
        self.assertNotEqual(done.returncode, 0)


if __name__ == "__main__":
    unittest.main()
