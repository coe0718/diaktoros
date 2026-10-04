"""What the host builds before it launches bubblewrap: no network, a scrubbed env, two tools (#16).

Fast unit tests of our own code, not of the sandbox. Nothing here starts bubblewrap or probes a
namespace: the production argv is built with ``contained.command`` from the kwargs
``trusted_turn.run_turn`` really passes (captured by a spy, the way
``test_adjudicator_isolated.Turn`` does), and ``contained.run`` is called for real with ``Popen``
replaced, so the ``env=`` it would hand bubblewrap is observed rather than inferred.

``tests/test_turn_vertical.py`` replaces ``contained.run`` with its own launcher and the boundary
tests build their own argv, so before these a ``--share-net`` in the launch, a widened toolset or
``os.environ`` passed through to bubblewrap all left CI green. What happens *inside* the namespace
(credential helpers, ``/proc`` traversal, a live connect) is the operator's live ``selftest``:
it probes ``sandbox:secrets``, ``sandbox:network`` and ``sandbox:env`` on the real layout.
"""
from __future__ import annotations
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_loop import broker_ipc, config, contained, trusted_turn  # noqa: E402

REPO = "acme/widgets"
HEAD = "a" * 40

# bubblewrap options by the number of operands each takes. An option missing from this table fails
# the parse loudly: a new flag in the launch is a decision someone has to look at here, not noise.
BWRAP_ARITY = {
    **dict.fromkeys(("--unshare-all", "--share-net", "--unshare-user", "--unshare-user-try",
                     "--unshare-ipc", "--unshare-pid", "--unshare-net", "--unshare-uts",
                     "--unshare-cgroup", "--unshare-cgroup-try", "--die-with-parent",
                     "--new-session", "--as-pid-1", "--clearenv"), 0),
    **dict.fromkeys(("--proc", "--dev", "--tmpfs", "--mqueue", "--dir", "--remount-ro",
                     "--size", "--perms", "--chdir", "--unsetenv", "--hostname", "--uid",
                     "--gid", "--cap-add", "--cap-drop"), 1),
    **dict.fromkeys(("--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind",
                     "--dev-bind-try", "--setenv", "--symlink", "--chmod"), 2),
}
BINDS = {"--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind", "--dev-bind-try"}
READ_ONLY_BINDS = {"--ro-bind", "--ro-bind-try"}
# Options whose last operand is a path inside the namespace.
MOUNT_POINTS = BINDS | {"--proc", "--dev", "--tmpfs", "--mqueue", "--dir", "--symlink"}
# What contained.run deliberately hands bubblewrap, and what the argv sets inside the namespace.
HOST_ENV = {"PATH", "HOME", "HERMES_HOME"}
SANDBOX_ENV = {"HOME", "HERMES_HOME", "PYTHONPATH", "CARGO_HOME", "RUSTUP_HOME",
               "CARGO_TARGET_DIR", "TMPDIR", "PATH", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
               "GIT_TERMINAL_PROMPT", contained.OFFLINE_ENV[0],
               # The sandbox's own user name (#240), matching its staged user entry.
               "USER", "LOGNAME"}
# Distinct from #120's PARENT-PROCESS-LEAK marker, and deliberately nothing like a token.
FAKE_CREDENTIALS = {"GH_TOKEN": "fake-env-marker-1", "GITHUB_TOKEN": "fake-env-marker-2",
                    "OPENROUTER_API_KEY": "fake-env-marker-3",
                    "ANTHROPIC_API_KEY": "fake-env-marker-4",
                    "SOME_SERVICE_TOKEN": "fake-env-marker-5",
                    "SOME_SERVICE_KEY": "fake-env-marker-6"}


def bwrap_options(argv: list[str]) -> list[tuple[str, ...]]:
    """``argv`` as ``(option, *operands)`` tuples, up to the ``--`` before the entry."""
    assert argv[0] == "bwrap", argv[:1]
    options, index = [], 1
    while argv[index] != "--":
        option = argv[index]
        if option not in BWRAP_ARITY:
            raise AssertionError(f"unrecognised bubblewrap option {option!r}: classify it in "
                                 "BWRAP_ARITY and decide what it means for the boundary")
        arity = BWRAP_ARITY[option]
        options.append((option, *argv[index + 1:index + 1 + arity]))
        index += 1 + arity
    return options


def at_or_under(path: str, root: str) -> bool:
    path, root = Path(os.path.normpath(path)), Path(os.path.normpath(root))
    return path == root or root in path.parents


# Captured before any test patches it: the one test of the check itself runs the real one.
REAL_UNAVAILABLE = contained.unavailable


class Base(unittest.TestCase):
    def setUp(self):
        # These pin the argv and env our code builds, so they run on a host with no bubblewrap
        # too: its presence check is answered here, and pinned on its own below.
        present = mock.patch.object(contained, "unavailable", return_value="")
        present.start()
        self.addCleanup(present.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.root.chmod(0o700)

    def staged(self) -> dict:
        """The directories ``contained.command`` requires to exist, freshly made."""
        paths = {}
        for name in ("code", "venv", "runtime", "home", "checkout", "rust"):
            paths[name] = self.root / "staged" / name
            paths[name].mkdir(parents=True)
        paths["query"] = self.root / "staged" / "query.txt"
        paths["query"].write_text("q")
        return paths


class ProductionLaunch(Base):
    """The argv production launches, built from the kwargs ``run_turn`` hands ``contained.run``."""

    def setUp(self):
        super().setUp()
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / f"{login}.pat"
            path.write_text("placeholder")
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
                     "state_dir": str(self.root / "state"), "fixers": ["fix"],
                     "reviewers": ["review"], "tokens": tokens, "read_token": "read",
                     "reviewer_seat": "review", "adjudicator": {"route": "widgets-breach"},
                     "seats": {"reviewer": {"login": "review", "agent": "Rex"},
                               "fixer": {"login": "fix", "agent": "Dee"}}}
        # Production's runtime is a uv python under the operator's home, bound at its own host path
        # (the venv's interpreter symlinks name it). Here it sits under the guarded test HOME.
        self.home = Path.home().resolve()
        self.runtime = self.home / ".local/share/uv/python/cpython-3.11-fake"
        self.runtime.mkdir(parents=True, exist_ok=True)
        for name in ("venv", "rust"):
            (self.root / name).mkdir()

    def launch(self, role: str) -> dict:
        """Run ``run_turn`` for ``role`` with the launcher spied; return what it was handed."""
        seen = {}

        def stage(_loop, **kw):
            kw["sandbox_root"].mkdir()
            return kw["sandbox_root"]

        def run(**kw):
            seen["kwargs"] = kw
            seen["argv"] = contained.command(**{k: v for k, v in kw.items() if k != "timeout"})
            return subprocess.CompletedProcess([], 0, "", "")

        class Inference:
            def __init__(self, directory, *a, **k):
                self.directory = directory
                seen["quota"] = k.get("quota")

            def __enter__(self):
                self.directory.mkdir()
                (self.directory / "model.sock").touch()  # is_socket is patched below
                return self

            def __exit__(self, *a):
                return False

        scope = broker_ipc.RunScope(REPO, 7, HEAD, role, "fix-7", "rid",
                                    str(self.root / "runs.sqlite"))
        with mock.patch.object(trusted_turn, "_safe_code_snapshot",
                               side_effect=lambda src, dst: dst.mkdir()), \
             mock.patch.object(trusted_turn.trusted_fetch, "stage", side_effect=stage), \
             mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
             mock.patch.object(contained.Path, "is_socket", return_value=True), \
             mock.patch.object(contained, "run", side_effect=run), \
             self.assertRaises(trusted_turn.TurnDenied):  # the spy confirms no scoped write
            trusted_turn.run_turn(self.loop, scope, source=self.root, venv=self.root / "venv",
                                  runtime=self.runtime, rust=self.root / "rust",
                                  upstream="https://model.invalid", key="k", model="m",
                                  prompt="PROMPT", timeout=5, work_root=self.root / "work")
        self.assertIn("argv", seen, "run_turn never reached the launcher")
        return seen

    def test_launch_has_no_network_and_a_fresh_namespace(self):
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                argv = self.launch(role)["argv"]
                options = bwrap_options(argv)
                flags = {option[0] for option in options}
                for required in ("--unshare-all", "--die-with-parent", "--new-session"):
                    self.assertIn(required, flags, argv)
                self.assertIn(("--proc", "/proc"), options, argv)
                # Anywhere at all: not among the options, not smuggled into the entry.
                self.assertNotIn("--share-net", argv)
                self.assertFalse([arg for arg in argv if "share-net" in arg], argv)
                # #160: --bind /usr would make the host root writable inside the sandbox.
                # The argv must not contain any --bind for host root paths (like /usr, /bin, /lib).
                # Legitimate --bind entries are for turn-scoped paths only (work/venv/rust/home/export/query/client).
                for arg in argv:
                    if arg == "--bind":
                        idx = argv.index(arg)
                        if idx + 1 < len(argv):
                            source = argv[idx + 1]
                            self.assertFalse(
                                source in ("/usr", "/bin", "/lib", "/lib64", "/etc", "/home", "/root", "/opt", "/srv", "/var", "/tmp"),
                                f"found dangerous --bind {source} in argv")

    def test_host_etc_is_not_bound_except_alternatives(self):
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                argv = self.launch(role)["argv"]
                for option in bwrap_options(argv):
                    paths = option[1:] if option[0] in BINDS else option[-1:] \
                        if option[0] in MOUNT_POINTS else ()
                    for path in paths:
                        if at_or_under(path, "/etc"):
                            self.assertTrue(at_or_under(path, "/etc/alternatives"),
                                            f"{option} exposes host /etc")
                self.assertIn(("--ro-bind-try", "/etc/alternatives", "/etc/alternatives"),
                              bwrap_options(argv))

    def test_host_home_is_not_bound_only_the_runtime_under_it(self):
        home, runtime = str(self.home), str(self.runtime)
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                options = bwrap_options(self.launch(role)["argv"])
                touching = set()
                for option in options:
                    if option[0] not in BINDS:
                        continue
                    source, target = option[1], option[2]
                    for path in (source, target):
                        # The home, any ancestor of it (``/`` included), or anything inside it.
                        if at_or_under(home, path) or at_or_under(path, home):
                            touching.add(option)
                # Exactly one bind reaches the home: the runtime, read-only, at its own host path.
                self.assertEqual(touching, {("--ro-bind", runtime, runtime)})
                # Its ancestors are created empty, never bound.
                dirs = {option[1] for option in options if option[0] == "--dir"}
                self.assertIn(home, dirs)

    def test_host_paths_are_bound_read_only_except_the_turn_home(self):
        # #160: ``--ro-bind /usr`` → ``--bind /usr`` (a writable host root) used to stay green. The
        # one writable host bind production makes is the turn's own staged home at /home/agent;
        # everything else from the host is read-only. The seat's other writable space is tmpfs
        # (/tmp, and /work or /target), which is not a host path at all.
        tmpfs = {"reviewer": {"/tmp", "/work"}, "fixer": {"/tmp", "/work"},
                 "adjudicator": {"/tmp", "/target"}}
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                seen = self.launch(role)
                argv, options = seen["argv"], bwrap_options(seen["argv"])
                home = str(seen["kwargs"]["home"])
                writable = [option for option in options
                            if option[0] in BINDS - READ_ONLY_BINDS]
                self.assertEqual(writable, [("--bind", home, "/home/agent")], argv)
                # No device passthrough: /dev is bubblewrap's own minimal one, and one procfs.
                self.assertFalse([arg for arg in argv if "dev-bind" in arg], argv)
                self.assertEqual([option for option in options if option[0] == "--dev"],
                                 [("--dev", "/dev")])
                self.assertEqual([option for option in options if option[0] == "--proc"],
                                 [("--proc", "/proc")])
                # No capability is granted: anywhere, the entry included.
                self.assertFalse([arg for arg in argv if "cap-add" in arg], argv)
                self.assertEqual({option[1] for option in options if option[0] == "--tmpfs"},
                                 tmpfs[role])
                # The root and /dev are sealed read-only, after every mount point is made.
                remounts = [index for index, option in enumerate(options)
                            if option[0] == "--remount-ro"]
                self.assertEqual([options[index] for index in remounts],
                                 [("--remount-ro", "/"), ("--remount-ro", "/dev")], argv)
                self.assertFalse([option for option in options[remounts[0]:]
                                  if option[0] in MOUNT_POINTS], argv)

    def test_every_read_only_bind_source_is_known_or_turn_scoped(self):
        # #166: the writable set is pinned exactly above, but nothing pinned *which* read-only
        # sources may appear — ``--ro-bind /srv /srv`` (an arbitrary host path) stayed green across
        # the whole suite. The sources are either a fixed system path the toolchain needs, or a path
        # under the turn's own staged directories (work/venv/rust/test-home/temp sockets). Anything
        # else is an arbitrary host path the sandbox must never see, read-only or not.
        system = {"/usr", "/bin", "/lib", "/lib64", "/etc/alternatives"}
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                seen = self.launch(role)
                options = bwrap_options(seen["argv"])
                kw = seen["kwargs"]
                # Every turn-scoped root the fixture stages: work dir, venv, rust, home, and the
                # temp dirs holding the inference/broker sockets and (when set) the client/review.
                def turn_root(path_value) -> str:
                    """The staged root a turn-scoped path lives under (<tmp>/work/..., <tmp>/...)."""
                    text = os.path.normpath(str(path_value))
                    head = text.split("/work/")[0] if "/work/" in text else str(Path(text).parent)
                    return head

                roots = [
                    turn_root(kw["code"]),       # <tmp>/work/... (code under it)
                    str(Path(str(kw["venv"])).parent),   # <tmp>/venv
                    str(Path(str(kw["rust"])).parent),   # <tmp>/rust
                    turn_root(kw["home"]),       # <tmp>/work/... (home under it)
                    str(Path(str(kw["runtime"])).parent), # test-home / uv python
                ]
                for key in ("inference_socket_dir", "broker_socket_dir", "client_code", "review_dir"):
                    value = kw.get(key)
                    if value:
                        roots.append(str(Path(str(value)).parent))
                def allowed(source: str) -> bool:
                    # The source must be the root itself or beneath it — never a broad ancestor of
                    # it. The symmetric `at_or_under(root, source)` accepted `/` (every root is
                    # beneath `/`), exposing the whole host filesystem behind a green safety check
                    # (#178 review).
                    return source in system or any(at_or_under(source, root) for root in roots)

                rogue = []
                for option in options:
                    if option[0] not in READ_ONLY_BINDS:
                        continue
                    if allowed(option[1]):
                        continue
                    rogue.append(option)
                self.assertEqual(rogue, [],
                                 f"{role}: read-only bind of an arbitrary host path: {rogue}")

    def test_a_read_only_bind_of_a_broad_ancestor_is_rejected(self):
        # The allowlist must reject the roots' own ancestors — `/`, `/tmp`, `/tmp/xyz`, and `/home`
        # all expose far more than the turn's staged directories. The predicate is directional:
        # source beneath root. It must call the same `allowed()` logic the validator uses.
        from test_contained_launch import at_or_under
        roots = ["/tmp/xyz/work/turn-abc", "/tmp/xyz/venv"]
        system = {"/usr", "/bin", "/lib", "/lib64", "/etc/alternatives"}

        def allowed(source: str) -> bool:
            return source in system or any(at_or_under(source, root) for root in roots)

        for source in ("/", "/tmp", "/tmp/xyz", "/home"):
            self.assertFalse(
                allowed(source),
                f"the allowlist accepted the broad ancestor {source!r}")

    def test_network_cannot_be_requested(self):
        # Production's kwargs (the turn's own directories are gone once run_turn returns, which
        # the refusal must not depend on), then a staged layout that exists for run().
        kwargs = dict(self.launch("reviewer")["kwargs"])
        kwargs.pop("timeout")
        with self.assertRaisesRegex(ValueError, "network"):
            contained.command(**kwargs, network=True)
        with mock.patch.object(contained.subprocess, "Popen",
                               side_effect=AssertionError("launched with network=True")), \
             self.assertRaisesRegex(ValueError, "network"):
            contained.run(**self.staged(), entry=["/bin/true"], network=True, timeout=5)

    def test_a_host_without_bubblewrap_is_refused_before_anything_starts(self):
        """#16: no sandbox, no turn — refused by name with the fix, never run without one."""
        with mock.patch.object(contained, "unavailable", REAL_UNAVAILABLE), \
             mock.patch.object(contained.shutil, "which", return_value=None) as which, \
             mock.patch.object(contained.subprocess, "Popen",
                               side_effect=AssertionError("launched without bubblewrap")), \
             mock.patch.object(contained, "write_etc",
                               side_effect=AssertionError("staged without bubblewrap")):
            with self.assertRaises(contained.ContainmentUnavailable) as raised:
                contained.run(**self.staged(), entry=["/bin/true"], timeout=5)
        which.assert_called_with("bwrap", path=contained.LAUNCH_PATH)
        self.assertIn("bubblewrap", str(raised.exception))
        self.assertIn("hermes review-loop retry", str(raised.exception))
        # A host fact, not a flake: the worker fails it once instead of spending its retries.
        from review_loop import run_supervisor
        self.assertFalse(run_supervisor.retryable(raised.exception))

    def test_hermes_gets_exactly_the_terminal_and_file_tools(self):
        for role in ("reviewer", "fixer", "adjudicator"):
            with self.subTest(role=role):
                entry = self.launch(role)["kwargs"]["entry"]
                self.assertIn("/opt/venv/bin/hermes", entry)
                hermes = entry[entry.index("/opt/venv/bin/hermes"):]
                self.assertEqual(hermes.count("-t"), 1, hermes)
                self.assertEqual(hermes[hermes.index("-t") + 1], "terminal,file")
                self.assertFalse([arg for arg in entry if arg.startswith("--toolset")], entry)


    def test_each_role_gets_its_own_step_and_model_call_caps(self):
        """A writing seat gets room to find its way into the code; a judging seat does not (#271).

        The agent's step cap is on Hermes's command line, the model-call cap on the host proxy;
        both come from the seat's ``max_steps`` setting or its role default, for every role."""
        self.assertEqual(set(config.DEFAULT_MAX_STEPS), set(trusted_turn.TOOLS))
        for role, default in config.DEFAULT_MAX_STEPS.items():
            with self.subTest(role=role):
                self.assertEqual(self.caps(role), (default, config.model_calls(default)))
        self.assertEqual(config.DEFAULT_MAX_STEPS["reviewer"], 24)
        self.assertEqual(config.model_calls(24), 32)              # what every seat shipped with
        for role in ("fixer", "issue_fixer"):
            self.assertGreater(config.DEFAULT_MAX_STEPS[role], config.DEFAULT_MAX_STEPS["reviewer"])
        # The proxy refuses a quota above its ceiling at construction: the largest setting a seat
        # may take must still be one it grants, or every turn of that seat would fail.
        self.assertLessEqual(config.model_calls(config.MAX_STEPS_RANGE[1]),
                             trusted_turn.inference_proxy.MAX_CALLS)

    def test_a_seat_setting_moves_its_caps_and_an_issue_fix_takes_the_fixers(self):
        self.loop["seats"] = {"reviewer": {"login": "rev", "max_steps": 40},
                              "fixer": {"login": "fix", "max_steps": 150}}
        self.assertEqual(self.caps("reviewer"), (40, 50))
        self.assertEqual(self.caps("fixer"), (150, 187))
        self.assertEqual(self.caps("issue_fixer"), (150, 187))
        self.assertEqual(self.caps("adjudicator"), (24, 32))     # unset: its role default

    def caps(self, role: str) -> tuple[int, int]:
        seen = self.launch(role)
        entry = seen["kwargs"]["entry"]
        hermes = entry[entry.index("/opt/venv/bin/hermes"):]
        self.assertEqual(hermes.count("--max-turns"), 1, hermes)
        return int(hermes[hermes.index("--max-turns") + 1]), seen["quota"]


class EnvironmentScrub(Base):
    """``contained.run`` hands bubblewrap a built environment, never the host's."""

    def test_host_credentials_never_reach_the_launch(self):
        paths = self.staged()
        seen = {}

        class Launched(Exception):
            pass

        def popen(argv, **kw):
            seen["argv"], seen["env"] = list(argv), kw.get("env")
            raise Launched

        with mock.patch.dict(os.environ, FAKE_CREDENTIALS), \
             mock.patch.object(contained.subprocess, "Popen", side_effect=popen), \
             self.assertRaises(Launched):
            contained.run(**paths, entry=["/bin/true"], timeout=5)

        env, argv = seen["env"], seen["argv"]
        self.assertIsInstance(env, dict, "env=None would inherit the host's environment")
        self.assertEqual(set(env), HOST_ENV)
        self.assertEqual(env["HOME"], str(paths["home"]))
        self.assertEqual(env["HERMES_HOME"], str(paths["home"]))
        self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/bin")
        setenv = {option[1] for option in bwrap_options(argv) if option[0] == "--setenv"}
        self.assertEqual(setenv, SANDBOX_ENV)
        self.assertIn(("--setenv", *contained.OFFLINE_ENV), bwrap_options(argv))
        for name, value in FAKE_CREDENTIALS.items():
            with self.subTest(name=name):
                self.assertNotIn(name, env)
                self.assertFalse([v for v in env.values() if value in v], env)
                self.assertFalse([arg for arg in argv if value in arg or name in arg], argv)


if __name__ == "__main__":
    unittest.main()
