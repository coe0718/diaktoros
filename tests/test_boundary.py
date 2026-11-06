"""Focused proof for fail-closed gates, broker guards, and a Rust-only namespace probe.

The bwrap probe does NOT prove an entire Hermes agent is contained: no agent is
launched by this plugin. It validates a feasible isolated compiler mount layout.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, gh, gate, isolation

# Seconds each cargo step of the namespace probe may take: room for a cold build on a slow hosted
# runner. A step past it is a skip that names the step, never a red main (#1778).
CARGO_BOUND = 240


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        # The gate resolves its run ledger and runtime from $HERMES_HOME. Pin it to this fixture:
        # an ambient home holding a diaktoros-runtime.json (an installed host, or a shared test
        # home another test wrote one into) turns the fail-closed hold into a real enqueue — a row
        # in that home's ledger and a detached production worker (#109).
        self.hermes = self.root / "hermes"
        self.hermes.mkdir()
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes)})
        env.start()
        self.addCleanup(env.stop)
        self.tokens = {}
        for role in ("read", "review", "fix"):
            path = self.root / (role + ".pat")
            path.write_text("dummy-" + role)
            self.tokens[role] = str(path)
        self.head = "a" * 40
        self.pr = {"number": 7, "state": "open", "draft": False,
                   "user": {"login": "fix"}, "head": {"sha": self.head,
                   "ref": "fix-7", "repo": {"full_name": "acme/widgets"}},
                   "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}}}
        self.loop = {"repo": "acme/widgets", "base": "main", "state_dir": str(self.root),
                     "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": self.tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.kw = dict(repo="acme/widgets", number=7, head=self.head, role="reviewer",
                       branch="fix-7", operation="review", verdict="APPROVE", body="verified")

    def test_broker_exact_binding_and_audit(self):
        calls = []
        def api(loop, path, method="GET", body=None, login=None):
            calls.append((path, method, body, login))
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
            return self.pr if method == "GET" else {"id": 8}
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': self.head,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}}]
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(gh, 'reviews', return_value=reviews):
            self.assertEqual(broker.perform(self.loop, **self.kw), {"id": 8})
            self.assertEqual(calls[-1][2]["commit_id"], self.head)
            self.assertEqual(calls[-1][3], "review")
            self.assertEqual(broker.perform(self.loop, **{**self.kw, "role": "fixer",
                              "operation": "request_review", "verdict": "", "body": ""}), {"id": 8})
            self.assertEqual(calls[-1][2], {"reviewers": ["review"]})
            self.assertEqual(calls[-1][3], "fix")
            before = len(calls)
            for changed in ({"repo": "other/widgets"}, {"number": 8},
                            {"branch": "elsewhere"}, {"head": "b" * 40},
                            {"role": "fixer"}, {"operation": "push"}):
                with self.subTest(changed=changed), self.assertRaises(broker.BrokerDenied):
                    broker.perform(self.loop, **{**self.kw, **changed})
            self.assertEqual(len([c for c in calls[before:] if c[1] == "POST"]), 0)
            self.pr["head"]["sha"] = "b" * 40
            with self.assertRaisesRegex(broker.BrokerDenied, "stale"):
                broker.perform(self.loop, **self.kw)
        records = (self.root / "broker-audit.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 2)
        self.assertEqual(json.loads(records[0])["branch"], "fix-7")
        self.assertNotIn("dummy-", "".join(records))

    def test_perform_refuses_authorized_operations_it_does_not_write(self):
        # push, answers and file_issue pass `authorize` for their roles, but `perform` has no
        # write for them: they must not fall through into a request-review POST (#737).
        calls = []
        def api(loop, path, method="GET", body=None, login=None):
            calls.append((path, method))
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
            return self.pr if method == "GET" else {"id": 8}
        reviews = [{'id': 41, 'state': 'CHANGES_REQUESTED', 'commit_id': self.head,
                    'submitted_at': '2026-01-01T00:00:00Z', 'user': {'login': 'review'}}]
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(gh, 'reviews', return_value=reviews):
            for role, operation in (("fixer", "push"), ("fixer", "answers"),
                                    ("reviewer", "file_issue")):
                with self.subTest(role=role, operation=operation):
                    with self.assertRaisesRegex(broker.BrokerDenied, "not performed"):
                        broker.perform(self.loop, **{**self.kw, "role": role,
                                       "operation": operation, "verdict": "", "body": ""})
        self.assertEqual([c for c in calls if c[1] == "POST"], [])
        self.assertFalse((self.root / "broker-audit.jsonl").exists())

    def test_missing_explicit_mapping_fails_before_api(self):
        with mock.patch.object(gh, "api") as api:
            for loop in ({**self.loop, "read_token": "absent"},
                         {**self.loop, "seats": {"reviewer": {"login": "absent"}}},
                         {**self.loop, "read_token": "review"},
                         {**self.loop, "tokens": {**self.tokens, "review": self.tokens["read"]}}):
                with self.assertRaises(broker.BrokerDenied):
                    broker.perform(loop, **self.kw)
            api.assert_not_called()

    def test_gate_blocks_before_workspace_or_gateway_payload(self):
        st = mock.Mock()
        st.queue_items.return_value = {}
        with mock.patch.object(gate.isolation, "ensure") as ensure:
            with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as stdout:
                with self.assertRaises(SystemExit) as caught:
                    gate.block_pr_agent(self.loop, st, "reviewer", 7, self.head)
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(stdout.getvalue().strip(), "[SILENT]")
            ensure.assert_not_called()
        st.queue_pop_if.assert_not_called()
        st.queue_replace_if.assert_called_once()
        self.assertIn("isolated worker unavailable", st.queue_replace_if.call_args.args[-1])
        self.assertFalse((self.hermes / "state").exists(), "no ledger without a runtime")

    def test_clone_removes_inherited_credential_helper(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "credential.helper",
                        "!f() { echo password=dummy-seat-token; }; f"], check=True)
        isolation._configure(repo, self.loop, "review")
        helper = subprocess.run(["git", "-C", str(repo), "config", "--get", "credential.helper"],
                                text=True, capture_output=True, check=True)
        self.assertEqual(helper.stdout.strip(), "")
        result = subprocess.run(["git", "-C", str(repo), "-c", "credential.interactive=false",
                                 "credential", "fill"],
                                input="protocol=https\nhost=github.com\nusername=unused\n\n", text=True,
                                capture_output=True, timeout=5,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                                     "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false"})
        self.assertNotIn("dummy-seat-token", result.stdout + result.stderr)

    def _isolation_loop(self):
        source = self.root / "source"
        (source / ".git").mkdir(parents=True)
        return {**self.loop, "id": "widgets", "clone": str(source),
                "state_dir": str(self.root / "state")}

    def test_isolation_refuses_when_clone_is_inside_root(self):
        loop = self._isolation_loop()
        root = isolation.paths(loop, 7, "reviewer")["root"]
        loop["clone"] = str(root / "repo")
        (root / "repo" / ".git").mkdir(parents=True)
        logged = []
        with mock.patch.object(isolation, "log", logged.append), \
                mock.patch.object(isolation, "_git") as git:
            self.assertIsNone(isolation.ensure(loop, 7, "reviewer"))
        git.assert_not_called()
        self.assertIn("is inside", logged[-1])

    def test_isolation_refuses_when_path_resolution_raises(self):
        # A symlink loop in the artifacts path makes resolve() raise; the clone-inside-root
        # check cannot be made, so isolation must be refused, not continued.
        loop = self._isolation_loop()
        artifacts = self.root / "state" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "7").symlink_to(artifacts / "7")
        logged = []
        with mock.patch.object(isolation, "log", logged.append), \
                mock.patch.object(isolation, "_git") as git:
            self.assertIsNone(isolation.ensure(loop, 7, "reviewer"))
        git.assert_not_called()
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("cannot tell whether", logged[0])

    def _rust_probe(self, toolchain, binds=(), cargo_bound=CARGO_BOUND):
        """Runs the probe in a fresh namespace with ``toolchain`` at /opt/rust. The boundary checks
        come first; each cargo step then has ``cargo_bound`` seconds (#1778)."""
        workspace = self.root / "work"
        (workspace / "src").mkdir(parents=True)
        (workspace / "Cargo.toml").write_text('[package]\nname="boundary_probe"\nversion="0.1.0"\nedition="2021"\n')
        (workspace / "src/lib.rs").write_text('#[test] fn works() { assert_eq!(2 + 2, 4); }\n')
        secret = self.root / "seat.pat"
        secret.write_text("dummy-seat-token")
        probe = self.root / "probe.py"
        probe.write_text('''import os, subprocess, sys
assert not os.path.exists(os.environ["HOST_PAT"])
for candidate in (os.environ["HOST_PAT"], "/work/../../" + os.environ["HOST_PAT"].lstrip("/"),
                  "/proc/self/root" + os.environ["HOST_PAT"], os.environ["HOST_ENV"]):
    try:
        open(candidate, "rb").read()
    except (FileNotFoundError, PermissionError):
        pass
    else:
        raise AssertionError("host path readable: " + candidate)
for path in ("/bin/sh", "/usr/bin/python3"):
    subprocess.run([path, "-c", "test ! -r $HOST_PAT" if path.endswith("sh") else "import os; assert not os.path.exists(os.environ['HOST_PAT'])"], check=True, timeout=30)
p = subprocess.run(["git", "credential", "fill"], input="protocol=https\\nhost=github.com\\n\\n", text=True, capture_output=True, timeout=5)
assert "dummy-seat-token" not in (p.stdout + p.stderr)
print("boundary: ok", flush=True)
bound = int(os.environ["CARGO_BOUND"])
for step in (["cargo", "--version"], ["cargo", "test", "--offline"]):
    try:
        subprocess.run(step, cwd="/work", check=True, timeout=bound)
    except subprocess.TimeoutExpired:
        print("cargo timeout: %s did not finish in %d s on this runner" % (" ".join(step), bound), flush=True)
        sys.exit(77)
''')
        cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
               "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
               "--ro-bind-try", "/lib64", "/lib64", "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
               "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/opt",
               "--ro-bind", str(toolchain), "/opt/rust", "--bind", str(workspace), "/work",
               "--ro-bind", str(probe), "/probe.py", *binds, "--setenv", "HOME", "/tmp",
               "--setenv", "CARGO_HOME", "/tmp/cargo", "--setenv", "RUSTUP_HOME", "/tmp/rustup",
               "--setenv", "HOST_PAT", str(secret),
               "--setenv", "HOST_ENV", str(_home_guard.USER_HOME / ".hermes/.env"),
               "--setenv", "CARGO_BOUND", str(cargo_bound),
               "--setenv", "PATH", "/opt/rust/bin:/usr/bin:/bin", "--chdir", "/work",
               "--", "/usr/bin/python3", "/probe.py"]
        overall = 2 * cargo_bound + 60
        try:
            return subprocess.run(cmd, text=True, capture_output=True, timeout=overall,
                                  env={"PATH": "/usr/bin:/bin", "HOME": str(self.root)})
        except subprocess.TimeoutExpired as late:
            self.fail(f"the probe did not finish in {overall} s; its output so far: {late.stdout!r}")

    def _rust_verdict(self, result):
        """Red for a boundary or cargo failure; a skip, with its reason, only for a host without
        user namespaces or a cargo step that ran out of time after the boundary checks passed."""
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        lines = result.stdout.splitlines()
        if result.returncode == 77 and "boundary: ok" in lines:
            after = lines[lines.index("boundary: ok") + 1:]       # only what cargo's steps printed
            timeout = [line for line in after if line.startswith("cargo timeout: ")]
            if timeout:
                self.skipTest(timeout[-1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("boundary: ok", result.stdout)
        self.assertIn("cargo ", result.stdout)
        self.assertIn("test result: ok", result.stdout)

    def _stuck_cargo(self, behaviour):
        """A toolchain whose ``cargo --version`` answers and whose ``cargo test`` does ``behaviour``."""
        toolchain = self.root / "stuck-rust"
        (toolchain / "bin").mkdir(parents=True)
        cargo = toolchain / "bin/cargo"
        cargo.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo "cargo 0.0.0 (stub)"; exit 0; fi\n'
                         + behaviour + "\n")
        cargo.chmod(0o755)
        return toolchain

    def _assert_red(self, result):
        """The verdict fails: a skip here would hide the failure, so it fails this test too."""
        try:
            self._rust_verdict(result)
        except unittest.SkipTest as skipped:
            self.fail(f"skipped, not red: {skipped}")
        except self.failureException:
            return
        self.fail("passed, not red")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_credentialless_rust_namespace_probe(self):
        stable = _home_guard.RUST
        if not (stable / "bin/cargo").exists():
            self.skipTest("stable Rust toolchain unavailable")
        self._rust_verdict(self._rust_probe(stable))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_a_cargo_step_out_of_time_is_a_skip_with_its_reason(self):
        """#1778: a slow runner's cargo is no regression; it skips, saying which step and bound."""
        result = self._rust_probe(self._stuck_cargo("exec sleep 600"), cargo_bound=2)
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        with self.assertRaises(unittest.SkipTest) as skipped:
            self._rust_verdict(result)
        self.assertEqual(str(skipped.exception),
                         "cargo timeout: cargo test --offline did not finish in 2 s on this runner")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_a_readable_host_path_is_red_even_when_cargo_would_stall(self):
        result = self._rust_probe(self._stuck_cargo("exec sleep 600"), cargo_bound=2,
                                  binds=("--ro-bind", str(self.root), str(self.root)))
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        self.assertIn("AssertionError", result.stderr)     # the seat token is visible
        self.assertNotIn("boundary: ok", result.stdout)
        self._assert_red(result)

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap unavailable")
    def test_a_failing_cargo_test_is_red_not_a_skip(self):
        result = self._rust_probe(self._stuck_cargo("echo 'test result: FAILED'; exit 101"))
        if "Creating new namespace failed" in result.stderr:
            self.skipTest("user namespaces disabled by host")
        self._assert_red(result)

    def test_only_a_timeout_after_the_boundary_checks_is_a_skip(self):
        """Review on #1977: each condition of the skip on its own. Exit 77, ``boundary: ok`` and
        a timeout line together skip; without any one of them the verdict is red."""
        ok, line = "boundary: ok\n", "cargo timeout: cargo test --offline did not finish in 240 s on this runner\n"

        def probe(code, stdout, stderr=""):
            return subprocess.CompletedProcess([], code, stdout, stderr)

        with self.assertRaises(unittest.SkipTest) as skipped:
            self._rust_verdict(probe(77, ok + "cargo 1.0\n" + line))
        self.assertEqual(str(skipped.exception), line.strip())
        cases = {
            "a boundary failure with a timeout line": probe(77, line, "AssertionError: host path readable"),
            "a cargo failure after the checks": probe(101, ok + line, "error: test failed"),
            "exit 77 with no timeout line": probe(77, ok + "cargo 1.0\n"),
            "a timeout line printed before the checks passed": probe(77, line + ok),
        }
        for name, result in cases.items():
            with self.subTest(name):
                self._assert_red(result)


if __name__ == "__main__":
    unittest.main()
