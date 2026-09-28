"""The suites never read or write the operator's real ~/.hermes, and an escape fails loudly.

``_home_guard`` gives every test process a temp HOME/HERMES_HOME and arms the plugin's tripwire;
these tests prove each half. The escape probes use a *fake* real home (``REVIEW_LOOP_TEST_REAL_HOME``
names a temp directory) so proving the tripwire never touches the operator's actual home.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import json
import os
import pathlib
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TESTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
from review_loop import (broker, cli, config, deps, doctor, gh, isolation, route_intent,  # noqa: E402
                         routes, safe_push, selftest, state, trusted_fetch)
from review_loop.run_supervisor import Supervisor  # noqa: E402

LEAKING = "test_boundary.BoundaryTests.test_gate_blocks_before_workspace_or_gateway_payload"
# A test that trusts the default home: it writes the run ledger wherever config.home() says. The
# boundary test above used to be that test until #115 pinned its HERMES_HOME, so the escape
# proof carries its own leak instead of depending on one still existing somewhere in the suite.
DEFAULT_HOME_WRITER = """
import _home_guard, sys
sys.path.insert(0, sys.argv[1])
from review_loop import config
from review_loop.run_supervisor import Supervisor
Supervisor(config.home() / "state" / "review-loop-runs.sqlite")
"""


def first_import(path: pathlib.Path) -> str:
    """The module imported by the first statement after the docstring and ``__future__``.

    ``""`` when that statement is not an import: an assignment, a call or a block before the
    guard import means code ran before the guard did.
    """
    body = ast.parse(path.read_text()).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.Import):
            return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            return node.module or ""
        return ""
    return ""


class HomeGuard(unittest.TestCase):
    def test_every_suite_imports_the_guard_first(self):
        files = [*sorted(TESTS.glob("test_*.py")), TESTS / "run_tests.py", TESTS / "harness/fixture.py"]
        late = [f.name for f in files if first_import(f) != "_home_guard"]
        self.assertEqual(late, [], "import _home_guard before anything else in these suites")

    def test_first_import_means_the_first_statement(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = pathlib.Path(tmp) / "test_x.py"
            late = ("X = 1\nimport _home_guard\n",
                    "import sys\nsys.path.insert(0, '.')\nimport _home_guard\n",
                    "print('side effect')\nimport _home_guard\n",
                    "if True:\n    import _home_guard\n")
            for body in late:
                module.write_text('"""doc"""\n' + body)
                self.assertNotEqual(first_import(module), "_home_guard", body)
            for body in ('"""doc"""\nimport _home_guard\nX = 1\n',
                         '"""doc"""\nfrom __future__ import annotations\nimport _home_guard\n',
                         'import _home_guard\n'):
                module.write_text(body)
                self.assertEqual(first_import(module), "_home_guard", body)

    def test_guard_is_armed_with_a_temp_home(self):
        real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        self.assertEqual(os.environ[config.TEST_HOME_GUARD_ENV], "1")
        self.assertNotEqual(pathlib.Path(os.environ["HOME"]).resolve(), real.resolve())
        self.assertEqual(pathlib.Path.home(), _home_guard.TEST_HOME)
        self.assertIn(_home_guard.TEST_HOME, config.home().resolve().parents)

    def test_tripwire_refuses_the_passwd_home(self):
        # Refused lexically, before anything under the real home is even stat'ed.
        real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        for hermes_home in (real, real / ".hermes", real / ".hermes/profiles/x"):
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                with self.assertRaises(config.RealHomeError):
                    config.home()
        with self.assertRaises(config.RealHomeError):
            Supervisor(real / ".hermes/state/review-loop-runs.sqlite")
        with self.assertRaises(config.RealHomeError):
            state.LoopState({"state_dir": str(real / ".hermes/state/review-loops/x")})
        with mock.patch.dict(os.environ, {config.TEST_HOME_GUARD_ENV: ""}):
            self.assertEqual(config.home(), pathlib.Path(os.environ["HERMES_HOME"]))

    def _run_leaking_test(self, fake_home: pathlib.Path, *, escaped: bool, argv=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "HOME")}
        env.update(HOME=str(fake_home), REVIEW_LOOP_TEST_REAL_HOME=str(fake_home))
        if escaped:
            # What an escape looks like: the guard believes it already ran, but HOME is the
            # "real" home and HERMES_HOME is unset, so config.home() defaults into it.
            env.update({config.TEST_HOME_GUARD_ENV: "1", "REVIEW_LOOP_TEST_USER_HOME": str(fake_home)})
        else:
            env.pop(config.TEST_HOME_GUARD_ENV, None)
            env.pop("REVIEW_LOOP_TEST_USER_HOME", None)
        argv = argv or ["-m", "unittest", "-v", LEAKING]
        return subprocess.run([sys.executable, *argv], cwd=TESTS, env=env,
                              text=True, capture_output=True, timeout=120)

    def test_escaping_test_hits_the_tripwire_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as fake:
            fake_home = pathlib.Path(fake)
            writer = ["-c", DEFAULT_HOME_WRITER, str(TESTS.parent)]
            result = self._run_leaking_test(fake_home, escaped=True, argv=writer)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("RealHomeError", result.stderr)
            self.assertEqual(list(fake_home.rglob("*")), [])
            # The same writer, properly guarded, succeeds, and in the temp home.
            result = self._run_leaking_test(fake_home, escaped=False, argv=writer)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(fake_home.rglob("*")), [])

    def test_previously_leaking_gate_test_uses_a_temp_home(self):
        with tempfile.TemporaryDirectory() as fake:
            fake_home = pathlib.Path(fake)
            result = self._run_leaking_test(fake_home, escaped=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(fake_home.rglob("*")), [])


# A fresh guarded process that launches a real fixture worker through Supervisor._spawn, which
# resolves the guarded HERMES_HOME strictly. Nothing else in that process has created it.
_SPAWN_A_WORKER = """
import _home_guard, os, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from review_loop.run_supervisor import SILENT, Supervisor
root = pathlib.Path(sys.argv[2])
child = root / "child.py"
child.write_text("import sys\\nopen(sys.argv[1], 'a').write('launched')\\n")
sup = Supervisor(root / "ledger.sqlite", fixture_mode=True,
                 fixture_command=[sys.executable, str(child), str(root / "launched")])
assert sup.enqueue("d", "o/r", 1, "sha", "reviewer") == SILENT
until = time.monotonic() + 30
while time.monotonic() < until and (sup.get("d") or {}).get("state") not in ("succeeded", "failed"):
    time.sleep(0.05)
print(os.environ["HERMES_HOME"], dict(sup.get("d")))
"""


class GuardedWorker(unittest.TestCase):
    def test_guarded_spawn_launches_and_completes_a_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "home").mkdir()
            env = {k: v for k, v in os.environ.items()
                   if k not in (config.TEST_HOME_GUARD_ENV, "REVIEW_LOOP_TEST_USER_HOME",
                                "HERMES_HOME", "REVIEW_LOOP_TEST_SHIM_DIR")}
            env.update(HOME=str(root / "home"), REVIEW_LOOP_TEST_REAL_HOME=str(root / "home"))
            result = subprocess.run([sys.executable, "-c", _SPAWN_A_WORKER, str(TESTS.parent), tmp],
                                    cwd=TESTS, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("'state': 'succeeded'", result.stdout, result.stdout + result.stderr)
            self.assertIn("/.hermes", result.stdout.split()[0])
            self.assertEqual((root / "launched").read_text(), "launched")
            self.assertEqual(list((root / "home").rglob("*")), [])

    def _guarded_child(self, home: pathlib.Path, hermes_home: pathlib.Path, real: pathlib.Path,
                       shim_dir: pathlib.Path | None = None):
        # By default no inherited shim dir: the child must place its own, as it would for a HOME
        # it made. With ``shim_dir``, the child inherits that one instead.
        env = {k: v for k, v in os.environ.items() if k != "REVIEW_LOOP_TEST_SHIM_DIR"}
        env.update({"HOME": str(home), "HERMES_HOME": str(hermes_home),
                    config.TEST_HOME_GUARD_ENV: "1", "REVIEW_LOOP_TEST_USER_HOME": str(real),
                    config.TEST_REAL_HOME_ENV: str(real)})
        if shim_dir is not None:
            env["REVIEW_LOOP_TEST_SHIM_DIR"] = str(shim_dir)
        return subprocess.run([sys.executable, "-c", "import _home_guard; print(_home_guard.SHIM_DIR)"],
                              cwd=TESTS, env=env, text=True, capture_output=True, timeout=60)

    def test_guarded_child_creates_the_homes_it_inherits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "real").mkdir()
            home, hermes_home = root / "home", root / "elsewhere/hermes"
            result = self._guarded_child(home, hermes_home, root / "real")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(home.is_dir())
            self.assertTrue(hermes_home.is_dir())
            self.assertEqual(list((root / "real").rglob("*")), [])

    def test_guarded_child_relocates_an_inherited_shim_dir_inside_the_real_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            real = root / "real"
            real.mkdir()
            # HOME outside the real home: only the inherited shim dir points into it.
            result = self._guarded_child(root / "home", root / "home/.hermes", real,
                                         shim_dir=real / ".hermes/bin")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(real.rglob("*")), [], result.stderr)
            shim_dir = pathlib.Path(result.stdout.strip())
            self.assertNotIn(real, [shim_dir, *shim_dir.parents])

    def test_guarded_child_relocates_an_inherited_shim_dir_anywhere_in_the_real_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            real = root / "real"
            real.mkdir()
            # Outside .hermes, but where config.guard_real_hermes calls a `hermes` the operator's.
            result = self._guarded_child(root / "home", root / "home/.hermes", real,
                                         shim_dir=real / ".local/bin")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(real.rglob("*")), [], result.stderr)
            shim_dir = pathlib.Path(result.stdout.strip())
            self.assertNotIn(real, [shim_dir, *shim_dir.parents])

    def test_relocated_shim_still_refuses_the_real_hermes(self):
        # The attack: a child inherits a shim dir inside the real home (so the shim is relocated)
        # with that dir first on PATH, and names the operator's hermes as its "fake".
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            real, marker = root / "real", root / "RAN-THE-REAL-HERMES"
            binaries = {}
            for where in (".hermes/bin", ".local/bin"):
                binary = real / where / "hermes"
                binary.parent.mkdir(parents=True)
                binary.write_text(f"#!/bin/sh\ntouch {marker}\necho THE REAL HERMES RAN\n")
                binary.chmod(0o755)
                binaries[where] = binary
            probe = ("import _home_guard, json, re, shlex, subprocess\n"
                     "text = (_home_guard.SHIM_DIR / 'hermes').read_text()\n"
                     "real = shlex.split(re.search(r'^real=(.*)$', text, re.M).group(1) or \"''\")\n"
                     "run = subprocess.run(['hermes', '--version'], capture_output=True, text=True)\n"
                     "print(json.dumps({'shim': str(_home_guard.SHIM_DIR), 'real': real[0] if real else '',"
                     " 'rc': run.returncode, 'out': run.stdout, 'err': run.stderr}))\n")
            for fake in binaries.values():
                with self.subTest(fake=str(fake.relative_to(real))):
                    env = {k: v for k, v in os.environ.items() if k != "REVIEW_LOOP_TEST_SHIM_DIR"}
                    env.update({"HOME": str(root / "home"), "HERMES_HOME": str(root / "home/.hermes"),
                                config.TEST_HOME_GUARD_ENV: "1",
                                "REVIEW_LOOP_TEST_USER_HOME": str(real),
                                config.TEST_REAL_HOME_ENV: str(real),
                                "REVIEW_LOOP_TEST_SHIM_DIR": str(real / ".hermes/bin"),
                                "PATH": os.pathsep.join([str(real / ".hermes/bin"),
                                                         str(real / ".local/bin"), "/usr/bin", "/bin"]),
                                _home_guard.FAKE_HERMES_ENV: str(fake)})
                    result = subprocess.run([sys.executable, "-c", probe], cwd=TESTS, env=env,
                                            text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    seen = json.loads(result.stdout)
                    self.assertEqual(seen["rc"], _home_guard.SHIM_EXIT, seen)
                    self.assertIn(_home_guard.BLOCKED, seen["err"])
                    self.assertNotIn("THE REAL HERMES RAN", seen["out"])
                    self.assertFalse(marker.exists())
                    # real= is the first non-shim hermes on the PATH with the shim dirs stripped.
                    self.assertEqual(seen["real"], str(binaries[".local/bin"]))

    def test_guarded_child_never_creates_a_home_anywhere_in_the_real_one(self):
        # HOME inherited as a subdirectory of the real home, outside its .hermes.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            real = root / "real"
            real.mkdir()
            result = self._guarded_child(real / "projects/x", root / "elsewhere/hermes", real)
            self.assertEqual(list(real.rglob("*")), [], result.stderr)
            self.assertTrue((root / "elsewhere/hermes").is_dir())   # the one outside still made

    def test_a_home_under_the_real_one_is_refused_by_the_plugin_on_first_use(self):
        # The guard declines to create an inherited HOME=<real>/projects/x; the plugin must then
        # refuse the write the guard declined, not recreate that tree itself (Supervisor's mkdir).
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real"
            real.mkdir()
            env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME",)}
            env.update({"HOME": str(real / "projects/x"), config.TEST_HOME_GUARD_ENV: "1",
                        "REVIEW_LOOP_TEST_USER_HOME": str(real), config.TEST_REAL_HOME_ENV: str(real)})
            result = subprocess.run([sys.executable, "-c", DEFAULT_HOME_WRITER, str(TESTS.parent)],
                                    cwd=TESTS, env=env, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("RealHomeError", result.stderr)
            self.assertEqual(list(real.rglob("*")), [])

    def test_guarded_child_never_creates_a_home_inside_the_real_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / "real"
            real.mkdir()
            result = self._guarded_child(real / ".hermes/home", real / ".hermes/profiles/x", real)
            self.assertEqual(list(real.rglob("*")), [], result.stderr)


class RealHomeWrites(unittest.TestCase):
    """Every write rooted in a loop's state_dir, or in an env-var override of a Hermes-home path,
    refuses a path inside the (fake) real home's .hermes — before anything is created there."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.real = pathlib.Path(self.temp.name) / "real"      # a sentinel "real home"
        self.real.mkdir()
        patch = mock.patch.dict(os.environ, {config.TEST_REAL_HOME_ENV: str(self.real)})
        patch.start()
        self.addCleanup(patch.stop)
        self.source = pathlib.Path(self.temp.name) / "source"   # a clone to isolate from
        (self.source / ".git").mkdir(parents=True)
        self.loop = {"id": "x", "repo": "acme/widgets",
                     "state_dir": str(self.real / ".hermes/state/review-loops/x")}

    def assert_refused(self, write):
        with self.assertRaises(config.RealHomeError):
            write()
        self.assertEqual(list(self.real.rglob("*")), [])

    def test_state_dir_writes_are_refused(self):
        loop = self.loop
        for name, write in (
                ("route_intent.record", lambda: route_intent.record(loop, {})),
                ("broker._audit", lambda: broker._audit(loop, "acme/widgets", 7, "a" * 40, "fix-7",
                                                        "fixer", "push", "fixer")),
                ("safe_push._audit", lambda: safe_push._audit(loop, {"operation": "push"})),
                ("config.artifacts_dir", lambda: config.artifacts_dir(loop, 7)),
                ("isolation.ensure", lambda: isolation.ensure({**loop, "clone": str(self.source)}, 7,
                                                              "reviewer")),
                ("selftest._work_root", lambda: selftest._work_root(loop)),
                ("deps.cache_root", lambda: deps.cache_root(loop))):
            with self.subTest(name):
                self.assert_refused(write)

    def test_anywhere_under_the_real_home_is_refused_not_only_its_hermes(self):
        for where in ("projects/x/state", "projects/x/.hermes", "."):
            with self.subTest(where):
                self.assert_refused(lambda: config.state_dir({"state_dir": str(self.real / where)}))

    def test_env_overrides_of_hermes_home_paths_are_refused(self):
        for var, resolve, target in (
                ("REVIEW_LOOP_CONFIG_DIR", config.config_dir, ".hermes/review-loops.d"),
                ("REVIEW_LOOP_SUBS", routes.subs_path, ".hermes/webhook_subscriptions.json")):
            with self.subTest(var), mock.patch.dict(os.environ, {var: str(self.real / target)}):
                self.assert_refused(resolve)


class TmpdirUnderHome(unittest.TestCase):
    """A TMPDIR under the (fake) real home must not put the temp home, or the shim, under it:
    guard_real_hermes refuses any `hermes` anywhere in the real home, so the shim would refuse
    itself. The guard picks a temp root outside the home instead."""

    def test_suite_runs_with_tmpdir_under_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "tmp").mkdir(parents=True)
            env = {k: v for k, v in os.environ.items()
                   if k not in (config.TEST_HOME_GUARD_ENV, "REVIEW_LOOP_TEST_USER_HOME",
                                "HERMES_HOME", "REVIEW_LOOP_TEST_SHIM_DIR")}
            env.update(HOME=str(home), TMPDIR=str(home / "tmp"),
                       REVIEW_LOOP_TEST_REAL_HOME=str(home))
            result = subprocess.run([sys.executable, "-m", "unittest", "test_home_guard.HermesShim"],
                                    cwd=TESTS, env=env, text=True, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list((home / "tmp").iterdir()), [])


class TripwireOutsideTheHarness(unittest.TestCase):
    """REVIEW_LOOP_TEST_HOME_GUARD alone — inherited by a real loop — must not brick it: the
    tripwire arms only with the guard's own sentinel, which only tests/_home_guard.py creates."""

    PROBE = ("import sys; sys.path.insert(0, sys.argv[1])\n"
             "from review_loop import config\n"
             "print(config.test_guard_active())\n"
             "print(config.home())\n"
             "print(config.guard_network('https://api.github.com/user'))\n")

    def _production(self, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / ".hermes").mkdir(parents=True)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
                   config.TEST_HOME_GUARD_ENV: "1", config.TEST_REAL_HOME_ENV: str(home), **extra}
            return home, subprocess.run([sys.executable, "-c", self.PROBE, str(TESTS.parent)],
                                        cwd=tmp, env=env, text=True, capture_output=True, timeout=60)

    def test_the_bare_variable_does_not_arm_the_tripwire(self):
        home, result = self._production()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\n")[:3],
                         ["False", str(home / ".hermes"), "https://api.github.com/user"])

    def test_a_stale_sentinel_does_not_arm_it_either(self):
        home, result = self._production(**{config.TEST_GUARD_SENTINEL_ENV: "/nonexistent/sentinel"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split("\n")[0], "False")

    def test_under_the_harness_it_is_armed_and_says_how_to_disarm(self):
        self.assertTrue(config.test_guard_active())
        real = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        with self.assertRaises(config.RealHomeError) as caught:
            config.guard_real_home(real / ".hermes")
        self.assertIn(f"test guard active ({config.TEST_HOME_GUARD_ENV}=1)", str(caught.exception))
        self.assertIn(f"unset {config.TEST_HOME_GUARD_ENV} if this is a real loop", str(caught.exception))
        with self.assertRaises(config.RealNetworkError) as caught:
            config.guard_network("https://api.github.com/user")
        self.assertIn(f"unset {config.TEST_HOME_GUARD_ENV} if this is a real loop", str(caught.exception))


class CratesIoAllowlist(unittest.TestCase):
    """The one sanctioned network path under the guard: an anonymous, credential-free crates.io
    fetch by deps._fetch (index.crates.io for the sparse index, static.crates.io for downloads).
    cargo is a subprocess, so the check is its effective registry configuration before it runs:
    anything that could point it elsewhere — a source replacement, another registry, a proxy —
    is refused."""

    LOCK = ('version = 4\n\n[[package]]\nname = "itoa"\nversion = "1.0.18"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        self.work, self.cache, self.home = root / "pkg", root / "cargo", root / "home"
        for directory in (self.work, self.cache, self.home):
            directory.mkdir()
        self.env = deps.cargo_env(self.home, self.cache, root / "rustc", root / "cargo-bin")
        self.manifest = deps.synthetic_manifest([("itoa", "1.0.18")])

    def check(self, env=None, manifest=None, lock=None):
        deps.guard_registry(self.env if env is None else env, self.work, self.cache,
                            self.manifest if manifest is None else manifest,
                            (self.LOCK if lock is None else lock).encode())

    def test_the_canonical_crates_io_fetch_is_allowed(self):
        self.assertTrue(config.test_guard_active())
        self.check()
        self.assertEqual(self.env["CARGO_REGISTRIES_CRATES_IO_PROTOCOL"], "sparse")
        self.assertEqual(deps.CRATES_IO_HOSTS, frozenset({"index.crates.io", "static.crates.io"}))

    def test_a_crate_named_like_a_registry_is_still_crates_io(self):
        # A real lockfile (patchhive/attest) pins signal-hook-registry: a crate *name*, not a key.
        for name in ("signal-hook-registry", "registry", "source", "patch-rs", "replace_me"):
            with self.subTest(name):
                lock = self.LOCK.replace('name = "itoa"', f'name = "{name}"')
                self.check(manifest=deps.synthetic_manifest([(name, "1.0.18")]), lock=lock)

    def test_manifest_registry_keys_and_sections_are_refused(self):
        for extra in ('x = { version = "1", registry = "x" }\n', 'y = { registry="x", version="1" }\n',
                      '[patch.crates-io]\nitoa = { path = "x" }\n',
                      '[source.crates-io]\nreplace-with = "m"\n', '[registries.m]\nindex = "x"\n',
                      '[replace]\n"itoa:1.0.18" = { path = "x" }\n'):
            with self.subTest(extra), self.assertRaises(config.RealNetworkError):
                self.check(manifest=self.manifest + extra)

    def test_another_registry_is_refused(self):
        cases = {
            "config in the package": lambda: (self.work / ".cargo").mkdir()
            or (self.work / ".cargo/config.toml").write_text(
                '[source.crates-io]\nreplace-with = "mirror"\n'
                '[source.mirror]\nregistry = "sparse+https://mirror.example/"\n'),
            "config in CARGO_HOME": lambda: (self.cache / "config.toml").write_text(
                '[registries.x]\nindex = "sparse+https://x.example/"\n'),
        }
        for name, plant in cases.items():
            with self.subTest(name):
                plant()
                with self.assertRaises(config.RealNetworkError):
                    self.check()
                for leftover in (self.work / ".cargo/config.toml", self.cache / "config.toml"):
                    leftover.unlink(missing_ok=True)
        refusals = {
            "registry env": dict(self.env, CARGO_REGISTRIES_X_INDEX="sparse+https://x.example/"),
            "source replacement env": dict(self.env, CARGO_SOURCE_CRATES_IO_REPLACE_WITH="x"),
            "git index protocol": dict(self.env, CARGO_REGISTRIES_CRATES_IO_PROTOCOL="git"),
        }
        for name, env in refusals.items():
            with self.subTest(name), self.assertRaises(config.RealNetworkError):
                self.check(env=env)
        with self.subTest("manifest names a registry"), self.assertRaises(config.RealNetworkError):
            self.check(manifest=self.manifest + 'x = { version = "1", registry = "x" }\n')
        with self.subTest("lockfile names another source"), self.assertRaises(config.RealNetworkError):
            self.check(lock=self.LOCK.replace("registry+https://github.com/rust-lang/crates.io-index",
                                              "sparse+https://x.example/"))

    def test_fetch_checks_before_cargo_runs(self):
        # Through deps._fetch itself: a redirecting config refuses, and cargo never starts.
        marker = self.home / "cargo-ran"
        cargo = self.home / "cargo"
        cargo.write_text(f"#!/bin/sh\ntouch {marker}\n")
        cargo.chmod(0o755)
        (self.cache / "config.toml").write_text('[source.crates-io]\nreplace-with = "m"\n')
        with self.assertRaises(config.RealNetworkError):
            deps._fetch(self.cache, [("itoa", "1.0.18")], self.LOCK.encode(), cargo,
                        self.home / "rustc", time.monotonic() + 30, 1 << 30, 30)
        self.assertFalse(marker.exists())
        (self.cache / "config.toml").unlink()          # the canonical fetch does reach cargo
        deps._fetch(self.cache, [("itoa", "1.0.18")], self.LOCK.encode(), cargo,
                    self.home / "rustc", time.monotonic() + 30, 1 << 30, 30)
        self.assertTrue(marker.exists())

    def test_a_proxy_is_refused(self):
        for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "ALL_PROXY", "CARGO_HTTP_PROXY"):
            with self.subTest(name), self.assertRaises(config.RealNetworkError):
                self.check(env=dict(self.env, **{name: "http://proxy.example:3128"}))

    def test_outside_the_guard_nothing_is_checked(self):
        with mock.patch.dict(os.environ, {config.TEST_HOME_GUARD_ENV: ""}):
            self.check(env=dict(self.env, https_proxy="http://proxy.example:3128"))

    def test_every_other_host_stays_blocked(self):
        for url in ("https://index.crates.io/", "https://static.crates.io/crates/itoa",
                    "https://crates.io/api/v1/crates"):
            with self.subTest(url), self.assertRaises(config.RealNetworkError):
                config.guard_network(url)


class HermesShim(unittest.TestCase):
    """No guarded test may run the operator's real ``hermes``; plugin code gets the shim."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)

    def test_hermes_on_path_is_the_shim_and_it_refuses(self):
        shim = _home_guard.SHIM_DIR / "hermes"
        self.assertEqual(shutil.which("hermes"), str(shim))
        self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(_home_guard.SHIM_DIR))
        result = subprocess.run([str(shim), "update"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, _home_guard.SHIM_EXIT)
        self.assertIn(_home_guard.BLOCKED, result.stderr)

    def test_plugin_cron_create_hits_the_shim_not_the_real_binary(self):
        # cli._install_schedule is the plugin's one PATH lookup of `hermes` (`init --schedule`).
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes")}):
            os.environ.pop(_home_guard.FAKE_HERMES_ENV, None)
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertFalse(ok)
        self.assertIn("cron create failed", lines[0])
        self.assertIn(_home_guard.BLOCKED, lines[0])

    def test_plugin_cron_create_runs_an_explicit_fake(self):
        record = self.root / "argv"
        fake = self.root / "fake-hermes"
        fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root / "hermes"),
                                          _home_guard.FAKE_HERMES_ENV: str(fake)}):
            lines, ok = cli._install_schedule({"id": "widgets"}, "15m", "local")
        self.assertTrue(ok)
        self.assertIn("scheduled the watchdog", lines[0])
        self.assertEqual(record.read_text().split("\n")[:5],
                         ["cron", "create", "15m", "--name", cli.watchdog_job_name({"id": "widgets"})])

    def test_a_fake_naming_the_real_binary_is_refused(self):
        # A stand-in "real hermes" that would leave a marker if the shim ever ran it.
        marker = self.root / "ran"
        real = self.root / "real-hermes"
        real.write_text(f"#!/bin/sh\ntouch {marker}\n")
        real.chmod(0o755)
        shim = self.root / "hermes"
        shim.write_text(_home_guard.shim_script(str(real), []))
        shim.chmod(0o755)
        result = subprocess.run([str(shim)], capture_output=True, text=True, timeout=10,
                                env={**os.environ, _home_guard.FAKE_HERMES_ENV: str(real)})
        self.assertEqual(result.returncode, _home_guard.SHIM_EXIT)
        self.assertIn("names the real binary", result.stderr)
        self.assertFalse(marker.exists())

    def test_a_hard_link_to_the_real_binary_is_refused(self):
        # Same inode, different path, outside every protected home. (A *copy* is a different
        # file: out of scope — the shim cannot tell a copied binary from a fake.)
        marker = self.root / "ran"
        real = self.root / "opt/bin/hermes"
        real.parent.mkdir(parents=True)
        real.write_text(f"#!/bin/sh\ntouch {marker}\n")
        real.chmod(0o755)
        link = self.root / "fakes/hermes"
        link.parent.mkdir()
        os.link(real, link)
        shim = self.root / "hermes"
        shim.write_text(_home_guard.shim_script(str(real), []))
        shim.chmod(0o755)
        result = subprocess.run([str(shim)], capture_output=True, text=True, timeout=10,
                                env={**os.environ, _home_guard.FAKE_HERMES_ENV: str(link)})
        self.assertEqual(result.returncode, _home_guard.SHIM_EXIT, result.stderr)
        self.assertIn("names the real binary", result.stderr)
        self.assertFalse(marker.exists())

    def test_plugin_refuses_a_hermes_inside_the_real_home(self):
        fake_home = self.root / "home"
        with mock.patch.dict(os.environ, {config.TEST_REAL_HOME_ENV: str(fake_home)}):
            with self.assertRaises(config.RealHomeError):
                config.guard_real_hermes(str(fake_home / ".local/bin/hermes"))
            self.assertEqual(config.guard_real_hermes(str(_home_guard.SHIM_DIR / "hermes")),
                             str(_home_guard.SHIM_DIR / "hermes"))



class LiveHermesSource(unittest.TestCase):
    """The opt-in real-Hermes tests refuse a HERMES_AGENT_SOURCE inside the live install."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name) / "home"          # a fake "real home"
        self.live = self.home / ".hermes/hermes-agent"
        self.marker = pathlib.Path(self.temp.name) / "fake-hermes-ran"
        hermes = self.live / "venv/bin/hermes"
        hermes.parent.mkdir(parents=True)
        hermes.write_text(f"#!/bin/sh\ntouch {self.marker}\n")
        hermes.chmod(0o755)
        patch = mock.patch.dict(os.environ, {config.TEST_REAL_HOME_ENV: str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_refusal_covers_the_live_tree_and_symlinks_into_it_only(self):
        self.assertIn("disposable", _home_guard.source_refusal(self.live))
        self.assertIn("disposable", _home_guard.source_refusal(self.home / ".hermes"))
        link = pathlib.Path(self.temp.name) / "checkout-link"
        link.symlink_to(self.live)
        self.assertIn("disposable", _home_guard.source_refusal(link))
        disposable = pathlib.Path(self.temp.name) / "hermes-agent-copy"
        disposable.mkdir()
        self.assertEqual(_home_guard.source_refusal(disposable), "")
        self.assertEqual(_home_guard.source_refusal(self.home / "src/hermes-agent"), "")

    def test_real_hermes_suites_fail_loudly_not_skip(self):
        env = {k: v for k, v in os.environ.items()
               if k not in (config.TEST_HOME_GUARD_ENV, "REVIEW_LOOP_TEST_USER_HOME", "HERMES_HOME")}
        env.update(HOME=str(self.home), HERMES_AGENT_SOURCE=str(self.live),
                   PYTHONPATH=os.pathsep.join([str(TESTS), str(TESTS.parent)]))
        suites = ["test_contained_agent", "test_inference_proxy", "test_turn_vertical",
                  "test_route_worker_vertical", "test_oauth_seats.RealHermesWireFormats"]
        result = subprocess.run([sys.executable, "-m", "unittest", *suites], cwd=TESTS.parent, env=env,
                                text=True, capture_output=True, timeout=120)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        # One refusal per real-Hermes test or class, none of them skipped.
        self.assertEqual(result.stderr.count("Point HERMES_AGENT_SOURCE at a disposable"), 5,
                         result.stderr)
        self.assertNotIn("skipped", result.stderr)
        self.assertFalse(self.marker.exists())



class NoRealGitHub(unittest.TestCase):
    """A partially mocked test fails loudly instead of reaching GitHub (or any real host).

    ``urlopen`` is replaced by a recorder in every test here, so even a regression never sends
    the dummy token anywhere: it only shows up as a recorded call.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        token = pathlib.Path(self.temp.name) / "dummy.pat"
        token.write_text("dummy-token")
        self.loop = {"repo": "acme/widgets", "read_token": "reader", "tokens": {"reader": str(token)}}
        self.opened = []

        def recorder(request, *args, **kwargs):
            self.opened.append(getattr(request, "full_url", request))
            raise OSError("recorder: no network in tests")
        for target in ("urllib.request.urlopen",):
            patch = mock.patch(target, recorder)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("REVIEW_LOOP_GH_STUB", None)

    def test_mocking_only_gh_api_still_cannot_reach_github_through_gh_fetch(self):
        with mock.patch.object(gh, "api", return_value=[]):
            with self.assertRaises(config.RealNetworkError) as caught:
                gh.open_prs_read(self.loop)          # reads through gh.fetch, not gh.api
        self.assertIn("https://api.github.com/repos/acme/widgets/pulls", str(caught.exception))
        self.assertEqual(self.opened, [])

    def test_trusted_fetch_request_is_refused(self):
        with self.assertRaises(config.RealNetworkError):
            trusted_fetch._request(self.loop, "/repos/acme/widgets/pulls/7", "reader", 10, "x")
        self.assertEqual(self.opened, [])

    def test_safe_push_refuses_the_github_remote(self):
        with self.assertRaises(config.RealNetworkError):
            safe_push._git_cas(self.loop, "acme/widgets", "fix-7", "a" * 40, [], "m", "reader",
                               {"name": "n", "email": "e@example.invalid"})

    def _source_repo(self) -> str:
        source = pathlib.Path(self.temp.name) / "source"
        source.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                      "commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
        return str(source)

    def test_isolation_fetch_from_github_is_refused_before_git_runs(self):
        loop = {**self.loop, "id": "widgets", "clone": self._source_repo(),
                "state_dir": str(pathlib.Path(self.temp.name) / "state")}
        ran, real_git = [], isolation._git

        def recording_git(*args, **kwargs):
            ran.append(args)
            if args[0] == "fetch":      # never let git itself reach the network, even when red
                return subprocess.CompletedProcess(args, 128, "", "blocked by the test")
            return real_git(*args, **kwargs)
        with mock.patch.object(isolation, "_git", recording_git):
            with self.assertRaises(config.RealNetworkError) as caught:
                isolation.ensure(loop, 7, "reviewer")
        self.assertIn("https://github.com/acme/widgets.git", str(caught.exception))
        self.assertEqual([args for args in ran if args[0] == "fetch"], [])

    def test_isolation_fetch_from_a_local_remote_still_runs(self):
        source = self._source_repo()
        clone = pathlib.Path(self.temp.name) / "clone"
        subprocess.run(["git", "clone", "-q", source, str(clone)], check=True, capture_output=True)
        self.assertEqual(isolation._fetch(clone).returncode, 0)

    def test_isolation_fetch_fails_closed_when_git_cannot_name_the_remote(self):
        clone = pathlib.Path(self.temp.name)
        # A git without `ls-remote --get-url` (rc 129), one that answers with nothing, and one
        # that fails yet prints a local, guard-approved URL: the exit status alone must refuse it.
        for probe in (subprocess.CompletedProcess([], 129, "", "usage: git ls-remote"),
                      subprocess.CompletedProcess([], 0, "", ""),
                      subprocess.CompletedProcess([], 128, f"{clone / 'remote.git'}\n", "fatal")):
            ran = []

            def stub_git(*args, **kwargs):
                ran.append(args[0])
                return probe if args[0] == "ls-remote" else subprocess.CompletedProcess(args, 0, "", "")
            with mock.patch.object(isolation, "_git", stub_git):
                with self.assertRaises(config.RealNetworkError):
                    isolation._fetch(clone)
            self.assertEqual(ran, ["ls-remote"], probe)

    def test_bare_hostnames_fail_closed(self):
        for url in ("github.com", "gateway", "api.github.com/user", "", "example.com/x.git"):
            with self.subTest(url=url), self.assertRaises(config.RealNetworkError):
                config.guard_network(url)
        for url in ("/abs/remote.git", "./rel/remote.git", "../rel/remote.git",
                    "file:///tmp/remote.git", "tcp://127.0.0.1:9", "http://localhost:8080/x"):
            with self.subTest(url=url):
                self.assertEqual(config.guard_network(url), url)

    def test_scp_style_remote_is_not_a_local_path(self):
        for url in ("git@github.com:acme/widgets.git", "github.com:acme/widgets.git"):
            with self.assertRaises(config.RealNetworkError):
                config.guard_network(url)
        self.assertEqual(config.guard_network("./relative:path"), "./relative:path")

    def test_doctor_gateway_probe_is_refused_for_a_real_host(self):
        for host in ("https://gateway.example.invalid", "gateway.example.invalid"):
            with mock.patch("socket.create_connection") as connect:
                with self.assertRaises(config.RealNetworkError):
                    doctor.gateway_reachable(host)
            connect.assert_not_called()
        self.assertFalse(doctor.gateway_reachable("http://127.0.0.1:1")[0])

    def test_loopback_fakes_keep_working(self):
        for url in ("http://127.0.0.1:8080/x", "http://localhost/x", "http://[::1]:9/x",
                    "file:///tmp/remote.git", "/tmp/remote.git"):
            self.assertEqual(config.guard_network(url), url)
        with mock.patch.object(gh, "API", "http://127.0.0.1:9"):
            _, error = gh.fetch(self.loop, "/user")
        self.assertIn("recorder", error)
        self.assertEqual(self.opened, ["http://127.0.0.1:9/user"])


if __name__ == "__main__":
    unittest.main()
