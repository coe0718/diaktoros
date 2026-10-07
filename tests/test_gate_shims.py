"""Issue #105: every loop route's script must resolve where the gateway actually looks.

Hermes's webhook gateway runs a route's bare ``script`` from ``get_hermes_home()/scripts`` of the
profile the route is served under (``_profile_scope`` → ``get_profile_dir(profile)``), and refuses
anything that resolves outside that directory. These tests install loops into a disposable
HERMES_HOME laid out like a real one (root + ``profiles/<name>``) and then resolve every route the
way the gateway does: with a local copy of ``_resolve_script_path`` always, and with Hermes's own
function too when its source is available (``HERMES_AGENT_SOURCE``, default
``~/.hermes/hermes-agent``) — run in a subprocess whose HOME and HERMES_HOME are the disposable
ones, never the live install.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import argparse
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, config, doctor, routes  # noqa: E402
# By file, not as ``tests.hermes_prereqs``: in CI's installed-mode lane Hermes's own ``tests``
# package is on PYTHONPATH and would shadow ours.
sys.path.insert(0, str(ROOT / "tests"))
from hermes_prereqs import skip_or_fail  # noqa: E402

SOURCE = pathlib.Path(os.environ.get("HERMES_AGENT_SOURCE")
                      or pathlib.Path.home() / ".hermes/hermes-agent")
FIX, REV, READER = "fix-acct", "rev-acct", "reader-acct"
REVIEW_KEY = "placeholder-review-key"   # a stand-in route key: tests never hold a real one


def gateway_home(root: pathlib.Path, profile) -> pathlib.Path:
    """``hermes_cli.profiles.get_profile_dir``: the root for ``default``, else profiles/<name>."""
    profile = profile if isinstance(profile, str) and profile.strip() else "default"
    return root if profile == "default" else root / "profiles" / profile


def gateway_resolve(home: pathlib.Path, script_value):
    """``webhook_filters._resolve_script_path`` (pinned f84db42a) with get_hermes_home() = home."""
    if not isinstance(script_value, str) or not script_value.strip():
        return None, "script path is empty"
    scripts_root = (home / "scripts").resolve()
    raw_text = os.path.expandvars(script_value.strip())
    if raw_text == "~/.hermes" or raw_text.startswith("~/.hermes/"):
        candidate = (home / raw_text[len("~/.hermes/"):]).resolve()
    else:
        raw = pathlib.Path(raw_text).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (scripts_root / raw).resolve()
    if not candidate.is_relative_to(scripts_root):
        return None, f"script path resolves outside {scripts_root}"
    if not candidate.exists():
        return None, f"script not found: {candidate}"
    return (candidate, None) if candidate.is_file() else (None, f"script path is not a file: {candidate}")


_REAL = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from gateway.platforms.webhook_filters import _resolve_script_path
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    out = []
    for profile, script in json.loads(sys.argv[2]):
        # What WebhookAdapter._profile_scope -> gateway.run._profile_runtime_scope does to the home.
        token = set_hermes_home_override(str(get_profile_dir(profile)))
        try:
            path, error = _resolve_script_path(script)
        finally:
            reset_hermes_home_override(token)
        out.append([str(path) if path else None, error])
    print(json.dumps(out))
""")


def real_python():
    venv = SOURCE / "venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def require_real_resolver(test: unittest.TestCase) -> None:
    """Skip without the Hermes source — or fail, under REVIEW_LOOP_REQUIRE_HERMES_SOURCE=1 (CI's
    installed-mode lane), via #117's shared ``hermes_prereqs.skip_or_fail``."""
    if not (SOURCE / "gateway" / "platforms" / "webhook_filters.py").exists():
        skip_or_fail(test, f"no Hermes source at {SOURCE}")


def real_resolve(test: unittest.TestCase, home_env: dict, pairs) -> list:
    """Hermes's own resolver, per (profile, script), under the disposable HOME/HERMES_HOME."""
    proc = subprocess.run([real_python(), "-c", _REAL, str(SOURCE), json.dumps(pairs)],
                          capture_output=True, text=True, timeout=120, env=home_env,
                          cwd=home_env["HOME"])
    if proc.returncode != 0:
        skip_or_fail(test, f"Hermes resolver not importable from {SOURCE}: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = pathlib.Path(temp.name).resolve()
        self.home = self.tmp / "home"
        self.hermes = self.home / ".hermes"           # the classic layout: root under $HOME
        self.env = {"HOME": str(self.home), "HERMES_HOME": str(self.hermes),
                    "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "diaktoros.d"),
                    "REVIEW_LOOP_SUBS": str(self.hermes / "webhook_subscriptions.json")}
        # GitHub is always the stub here: no test in this file may reach the network.
        stub = self.tmp / "gh-world"
        stub.write_text('#!/bin/sh\ncase "$1" in */hooks*) echo "[]";; *) echo "{}";; esac\n')
        stub.chmod(0o755)
        self.env["REVIEW_LOOP_GH_STUB"] = str(stub)
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hermes.mkdir(parents=True)
        (self.hermes / "config.yaml").write_text("model: {}\n")
        for profile in ("critic", "coder", "arbiter"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        keys = self.home / "keys"
        keys.mkdir()
        self.pats = {}
        for name in ("read", "rev", "fix"):
            path = keys / f"{name}.pat"
            path.write_text(f"pat-fixture-{name}\n")
            path.chmod(0o600)
            self.pats[name] = path
        self.addCleanup(setattr, cli, "_SETTINGS", getattr(cli, "_SETTINGS", {}))

    def run_cli(self, argv, settings=None):
        ctx = _Ctx()
        cli.register_cli(ctx, settings=settings or {})
        parser = argparse.ArgumentParser(prog="hermes dk")
        ctx.setup(parser)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            args = parser.parse_args(argv)
            rc = args.func(args)
        return rc, out.getvalue()

    def init_argv(self, repo="acme/widgets", *extra):
        return ["init", "--repo", repo, "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "critic", "--fixer-profile", "coder",
                "--read-token", READER, "--host", "https://gateway.example",
                "--token", f"{READER}={self.pats['read']}",
                "--token", f"{REV}={self.pats['rev']}",
                "--token", f"{FIX}={self.pats['fix']}", *extra]

    def install(self, repo="acme/widgets", *extra):
        rc, out = self.run_cli(self.init_argv(repo, *extra))
        self.assertEqual(rc, 0, out)
        return out

    def full_install(self):
        return self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                            "--adjudicator-profile", "arbiter", "--observer-profile", "default")

    def observer_install(self):
        return self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                            "--adjudicator-profile", "default", "--observer-profile", "arbiter")

    def loop_routes(self, loop_id="widgets") -> dict:
        path = pathlib.Path(self.env["REVIEW_LOOP_SUBS"])
        registry = json.loads(path.read_text()) if path.exists() else {}
        return {name: entry for name, entry in registry.items() if name.startswith(loop_id + "-")}


class GatewayResolvesEveryRoute(Base):
    def test_init_puts_every_gate_where_the_gateway_looks(self):
        self.full_install()
        entries = self.loop_routes()
        self.assertEqual(set(entries), {"widgets-review", "widgets-fix", "widgets-breach",
                                        "widgets-observe"})
        for name, entry in entries.items():
            home = gateway_home(self.hermes, entry.get("profile", "default"))
            path, error = gateway_resolve(home, entry["script"])
            self.assertIsNone(error, f"{name}: the gateway would drop every event: {error}")
            self.assertFalse((home / "scripts" / entry["script"]).is_symlink())
        # Each seat's gate lives in that seat's own profile home, not the root's.
        self.assertTrue((self.hermes / "profiles/critic/scripts/gate_reviewer.py").is_file())
        self.assertTrue((self.hermes / "profiles/coder/scripts/gate_fixer.py").is_file())
        self.assertTrue((self.hermes / "profiles/arbiter/scripts/gate_adjudicator.py").is_file())
        self.assertTrue((self.hermes / "scripts/observe.py").is_file())

    def test_hermes_own_resolver_agrees(self):
        require_real_resolver(self)
        self.full_install()
        entries = self.loop_routes()
        pairs = [[entry.get("profile", "default"), entry["script"]] for entry in entries.values()]
        real = real_resolve(self, {**os.environ, **self.env}, pairs)
        for (profile, script), (path, error) in zip(pairs, real):
            self.assertIsNone(error, f"{profile}/{script}: {error}")
            self.assertEqual(pathlib.Path(path),
                             (gateway_home(self.hermes, profile) / "scripts" / script).resolve())

    def test_local_resolver_matches_hermes_on_the_edge_cases(self):
        """The copy doctor uses must fail exactly where the gateway fails."""
        require_real_resolver(self)
        from review_loop import gate_shims
        scripts = self.hermes / "profiles" / "critic" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "real.py").write_text("print(1)\n")
        (scripts / "adir").mkdir()
        outside = self.tmp / "outside.py"
        outside.write_text("print(1)\n")
        (scripts / "link.py").symlink_to(outside)
        (scripts / "inner-link.py").symlink_to(scripts / "real.py")
        cases = ["real.py", "missing.py", "adir", "link.py", "inner-link.py", "../config.yaml",
                 str(outside), str(scripts / "real.py"), "~/.hermes/profiles/critic/scripts/real.py",
                 "", "  "]
        pairs = [[profile, case] for profile in ("critic", "default") for case in cases]
        real = real_resolve(self, {**os.environ, **self.env}, pairs)
        for (profile, case), want in zip(pairs, real):
            home = gateway_home(self.hermes, profile)
            for ours in (gate_shims.resolve(home, case), gateway_resolve(home, case)):
                got = [str(ours[0]) if ours[0] else None, ours[1]]
                self.assertEqual(got, want, f"{profile}: {case!r}")

    def test_dry_run_names_the_shims_and_writes_none(self):
        rc, out = self.run_cli(self.init_argv("acme/widgets"))  # a real loop for the id
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(self.init_argv("acme/gizmos", "--id", "gizmos",
                                              "--reviewer-profile", "arbiter", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"would write: {self.hermes / 'profiles/arbiter/scripts/gate_reviewer.py'}", out)
        self.assertFalse((self.hermes / "profiles/arbiter/scripts").exists())

    def test_foreign_file_is_refused_before_anything_is_written(self):
        scripts = self.hermes / "profiles" / "critic" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "gate_reviewer.py").write_text("print('mine')\n")
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 2, out)
        self.assertIn("not written by Diaktoros", out)
        self.assertEqual((scripts / "gate_reviewer.py").read_text(), "print('mine')\n")
        self.assertFalse((config.config_dir() / "widgets.json").exists())
        self.assertEqual(self.loop_routes(), {})

    def test_init_is_idempotent_and_rewrites_a_stale_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        before = shim.stat()
        self.assertEqual(gate_shims.install(config.load_id("widgets")), [])
        self.assertEqual(shim.stat().st_mtime_ns, before.st_mtime_ns)
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target="/old/plugin/x.py",
                                               home=str(self.hermes)))
        lines = gate_shims.install(config.load_id("widgets"))
        self.assertEqual(lines, [f"gate shim rewrote: {shim}"])
        self.assertEqual(shim.read_text(), gate_shims.render("gate_reviewer.py"))


_GATEWAY_ENV = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments.local import build_subprocess_env
    # What the gateway does before running a route's script: enter the serving profile's scope
    # (WebhookAdapter._profile_scope), then build the child env (webhook_filters.run_route_script).
    token = set_hermes_home_override(str(get_profile_dir(sys.argv[2])))
    try:
        print(json.dumps(build_subprocess_env()))
    finally:
        reset_hermes_home_override(token)
""")


class ShimRunsUnderTheLoopsHome(Base):
    """The gateway runs a ``/p/<profile>/`` route's script with HERMES_HOME = that profile's home.
    The loop lives under the root, so the shim must run the gate there. Without it, on a real
    gateway, every profile-routed gate found no loop and answered [SILENT], and the gateway does
    not log a script that exits 0 (#209)."""

    def probe(self) -> pathlib.Path:
        probe = self.tmp / "plugin" / "scripts" / "gate_reviewer.py"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(textwrap.dedent(f"""
            import json, os, sys
            sys.path.insert(0, {str(ROOT)!r})
            from review_loop import config
            print(json.dumps({{"hermes_home": os.environ.get("HERMES_HOME"),
                              "home": os.environ.get("HOME"),
                              "tmp": [os.environ.get(v) for v in ("TMPDIR", "TMP", "TEMP")],
                              "config_dir": str(config.config_dir())}}))
        """))
        return probe

    def run_shim(self, env: dict) -> dict:
        from review_loop import gate_shims
        with patch.object(gate_shims, "plugin_script", return_value=self.probe()):
            text = gate_shims.render("gate_reviewer.py")
        shim = self.hermes / "profiles" / "critic" / "scripts" / "gate_reviewer.py"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(text)
        proc = subprocess.run([sys.executable, str(shim)], input="{}", capture_output=True,
                              text=True, cwd=shim.parent, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_gate_reads_the_root_loops_under_a_profile_scoped_env(self):
        profile = self.hermes / "profiles" / "critic"
        # The gateway's child env: the profile as HERMES_HOME, HOME moved, the real one alongside;
        # and no REVIEW_LOOP_CONFIG_DIR, which only tests ever set.
        env = {"PATH": os.environ["PATH"], "HERMES_HOME": str(profile),
               "HOME": str(profile / "home"), "HERMES_REAL_HOME": str(self.home)}
        seen = self.run_shim(env)
        self.assertEqual(seen["hermes_home"], str(self.hermes))
        self.assertEqual(seen["config_dir"], str(self.hermes / "diaktoros.d"))
        self.assertEqual(seen["home"], str(self.home))
        scratch = str(self.hermes / "cache" / "scratch")
        self.assertEqual(seen["tmp"], [scratch] * 3)

    def test_with_hermes_own_gateway_env(self):
        """The same, with the env Hermes itself builds for a critic-scoped route script."""
        if not (SOURCE / "tools" / "environments" / "local.py").exists():
            skip_or_fail(self, f"no Hermes source at {SOURCE}")
        base = {"PATH": os.environ["PATH"], "HOME": str(self.home), "HERMES_HOME": str(self.hermes)}
        built = subprocess.run([real_python(), "-c", _GATEWAY_ENV, str(SOURCE), "critic"],
                               capture_output=True, text=True, timeout=120, env=base,
                               cwd=str(self.home))
        if built.returncode != 0:
            skip_or_fail(self, f"Hermes not importable from {SOURCE}: {built.stderr[-300:]}")
        env = json.loads(built.stdout)
        env.pop("REVIEW_LOOP_CONFIG_DIR", None)
        self.assertNotEqual(env.get("HERMES_HOME"), str(self.hermes),
                            "premise: Hermes scopes the script's HERMES_HOME to the profile")
        seen = self.run_shim(env)
        self.assertEqual(seen["config_dir"], str(self.hermes / "diaktoros.d"))


class ShimRunsThePluginScript(Base):
    def stub(self) -> pathlib.Path:
        stub = self.tmp / "gh-stub"
        stub.write_text("#!/bin/sh\necho '{}'\n")
        stub.chmod(0o755)
        return stub

    def sh(self, argv, cwd, payload: str):
        env = {**os.environ, **self.env, "REVIEW_LOOP_GH_STUB": str(self.stub())}
        return subprocess.run(argv, input=payload, capture_output=True, text=True, cwd=cwd,
                              env=env, timeout=60)

    def test_shim_is_indistinguishable_from_the_plugin_script(self):
        self.full_install()
        observed = {"_observer": {"message": "PR #7 opened", "event": "opened", "loop": "widgets",
                                  "pr": 7}, "noise": "dropped"}
        cases = [
            ("observe.py", self.hermes, json.dumps(observed)),
            ("observe.py", self.hermes, json.dumps({"action": "opened"})),
            ("gate_reviewer.py", self.hermes / "profiles/critic",
             json.dumps({"action": "synchronize", "repository": {"full_name": "acme/widgets"},
                         "pull_request": {"number": 7, "head": {"sha": "a" * 40}}})),
            ("gate_fixer.py", self.hermes / "profiles/coder", "not json"),
            ("gate_adjudicator.py", self.hermes / "profiles/arbiter",
             json.dumps({"action": "opened", "repository": {"full_name": "acme/unknown"}})),
        ]
        for script, home, payload in cases:
            with self.subTest(script=script, payload=payload[:30]):
                shim = home / "scripts" / script
                real = ROOT / "scripts" / script
                via_shim = self.sh([sys.executable, str(shim)], shim.parent, payload)
                direct = self.sh([sys.executable, str(real)], real.parent, payload)
                self.assertEqual((via_shim.returncode, via_shim.stdout),
                                 (direct.returncode, direct.stdout))
                if direct.returncode == 0:
                    self.assertEqual(via_shim.stderr, direct.stderr)
        narrowed = self.sh([sys.executable, str(self.hermes / "scripts/observe.py")],
                            self.hermes / "scripts", json.dumps(observed))
        self.assertEqual(json.loads(narrowed.stdout)["_observer"]["message"], "PR #7 opened")

    def test_file_path_argv_cwd_stdin_and_exit_code(self):
        from review_loop import gate_shims
        plugin = self.tmp / "plugin" / "scripts"
        plugin.mkdir(parents=True)
        probe = plugin / "probe.py"
        probe.write_text(textwrap.dedent("""
            import json, os, sys
            print(json.dumps({"file": __file__, "name": __name__, "path0": sys.path[0],
                              "argv0": sys.argv[0], "cwd": os.getcwd(),
                              "stdin": sys.stdin.read(),
                              "main": sys.modules["__main__"].__dict__.get("__file__")}))
            sys.exit(3)
        """))
        shim_dir = self.tmp / "profile" / "scripts"
        shim_dir.mkdir(parents=True)
        shim = shim_dir / "probe.py"
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target=str(probe),
                                               home=str(self.hermes)))
        via_shim = self.sh([sys.executable, str(shim)], shim_dir, "payload-bytes")
        direct = self.sh([sys.executable, str(probe)], plugin, "payload-bytes")
        self.assertEqual(via_shim.returncode, 3, via_shim.stderr)
        self.assertEqual(json.loads(via_shim.stdout), json.loads(direct.stdout))
        self.assertEqual(json.loads(direct.stdout)["file"], str(probe))

    def test_missing_plugin_script_fails_loudly(self):
        from review_loop import gate_shims
        shim = self.tmp / "shim.py"
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER,
                                               target=str(self.tmp / "gone.py"),
                                               home=str(self.hermes)))
        proc = self.sh([sys.executable, str(shim)], self.tmp, "{}")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("gate script missing", proc.stderr)


class SetAdjudication(Base):
    """``set --adjudicator-profile`` / ``--adjudicator off`` (issue #255)."""

    def checks(self):
        loop = config.load_id("widgets")
        return {c.name: c for c in doctor.check_loop(loop, offline=True)
                if c.name.startswith("gateway-script:")}

    def test_on_creates_route_block_and_shim_and_off_removes_them(self):
        self.install("acme/widgets")
        self.assertNotIn("widgets-breach", self.loop_routes())
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "arbiter"])
        self.assertEqual(rc, 0, out)
        loop = config.load_id("widgets")
        self.assertEqual(loop["adjudicator"], {"route": "widgets-breach", "profile": "arbiter"})
        entry = self.loop_routes()["widgets-breach"]
        self.assertEqual(entry["script"], "gate_adjudicator.py")
        shim = self.hermes / "profiles/arbiter/scripts/gate_adjudicator.py"
        self.assertTrue(shim.is_file())
        checks = self.checks()
        self.assertIn("gateway-script:widgets-breach", checks)
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)
        # Again: nothing to do.
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "arbiter"])
        self.assertEqual(rc, 0, out)
        self.assertIn("already on", out)

        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator", "off"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(config.load_id("widgets")["adjudicator"], {})
        self.assertNotIn("widgets-breach", self.loop_routes())
        self.assertFalse(shim.exists())
        self.assertTrue(all(c.status == doctor.VERIFIED for c in self.checks().values()))

    def test_route_override_and_refusals(self):
        self.install("acme/widgets")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "arbiter",
                                "--adjudicator-route", "widgets-ruling"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(config.load_id("widgets")["adjudicator"]["route"], "widgets-ruling")
        self.assertIn("widgets-ruling", self.loop_routes())
        # A seat's profile cannot rule on itself.
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "critic"])
        self.assertEqual(rc, 2, out)
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-profile", "nosuch"])
        self.assertEqual(rc, 2, out)

    def test_login_refusal_names_the_new_command(self):
        self.install("acme/widgets")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--adjudicator-login", "bot",
                                "--token", f"bot={self.pats['read']}"])
        self.assertEqual(rc, 2, out)
        self.assertIn("--adjudicator-profile", out)

    def test_setup_answer_flows_to_init(self):
        argv = []
        args = argparse.Namespace(
            reviewer="rev", fixer="fix", reviewer_profile="critic", fixer_profile="coder",
            reviewer_token="", fixer_token="", read_token="reader", read_token_file="",
            host="https://gateway.example", admin_token="", admin_token_file="",
            observer_profile="", review_after_ci="off", attribution="on", fixer_check="",
            required_check=[], adjudicator_profile="arbiter")
        argv, _admin = cli._setup_init_argv(args, "acme/widgets", "widgets", False)
        self.assertIn("--adjudicator-route=widgets-breach", argv)
        self.assertIn("--adjudicator-profile=arbiter", argv)
        args.adjudicator_profile = ""
        argv, _admin = cli._setup_init_argv(args, "acme/widgets", "widgets", False)
        self.assertFalse([a for a in argv if "adjudicator" in a])


class DoctorApplyUninstall(Base):
    def gateway_checks(self, loop_id="widgets"):
        loop = config.load_id(loop_id)
        return {c.name: c for c in doctor.check_loop(loop, offline=True)
                if c.name.startswith("gateway-script:")}

    def test_doctor_verifies_every_route_of_a_full_install(self):
        self.full_install()
        checks = self.gateway_checks()
        self.assertEqual(set(checks), {f"gateway-script:widgets-{r}"
                                       for r in ("review", "fix", "breach", "observe")})
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)
        (self.hermes / "scripts" / "observe.py").unlink()
        self.assertEqual(self.gateway_checks()["gateway-script:widgets-observe"].status,
                         doctor.ABSENT)

    def test_doctor_resolves_like_the_gateway_and_apply_repairs(self):
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "arbiter")
        checks = self.gateway_checks()
        self.assertEqual(set(checks), {f"gateway-script:widgets-{r}"
                                       for r in ("review", "fix", "breach")})
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)

        shim = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        shim.unlink()
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn(f"script not found: {shim}", check.detail)
        self.assertIn("hermes dk apply --loop widgets", check.fix)

        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"would write: {shim}", out)
        self.assertFalse(shim.exists())
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"gate shim wrote: {shim}", out)
        self.assertEqual(self.gateway_checks()["gateway-script:widgets-review"].status,
                         doctor.VERIFIED)

        shim.write_text("print('someone else')\n")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertEqual(shim.read_text(), "print('someone else')\n")

        link = self.hermes / "profiles/coder/scripts/gate_fixer.py"
        link.unlink()
        link.symlink_to(ROOT / "scripts" / "gate_fixer.py")
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn("resolves outside", check.detail)

    # -- config and registry disagree (review of #106): loud, and the named remedy works -------

    def edit_registry(self, mutate):
        path = pathlib.Path(self.env["REVIEW_LOOP_SUBS"])
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2))

    def edit_config(self, mutate):
        path = config.config_dir() / "widgets.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def doctor_out(self):
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline"])
        return out

    def test_hand_edited_registry_profile_is_a_mismatch_and_repair_fixes_it(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="arbiter"))
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("registry runs arbiter/gate_reviewer.py", check.detail)
        self.assertIn("loop config says critic/gate_reviewer.py", check.detail)
        self.assertIn("hermes dk doctor --loop widgets --repair", check.fix)
        # Never silent: install writes the shim the gateway will run *and* says the two disagree.
        from review_loop import gate_shims
        lines = gate_shims.install(config.load_id("widgets"))
        self.assertIn(f"gate shim wrote: {self.hermes / 'profiles/arbiter/scripts/gate_reviewer.py'}",
                      lines)
        self.assertTrue(any("registry runs arbiter/gate_reviewer.py" in line for line in lines), lines)
        # The named remedy, end to end.
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIn("widgets-review: had changed profile", out)
        self.assertEqual(routes.route("widgets-review")["profile"], "critic")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_hand_edited_config_profile_is_a_mismatch_and_apply_fixes_it(self):
        self.install()
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("registry runs critic/gate_reviewer.py", check.detail)
        self.assertIn("loop config says arbiter/gate_reviewer.py", check.detail)
        self.assertIn("hermes dk apply --loop widgets", check.fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "arbiter")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_blank_config_profile_is_a_mismatch_and_its_remedy_works(self):
        """A seat with no profile: the loader refuses such a file, so this is the in-memory shape a
        pre-validation loop has. The registry still routes the seat, so the gateway still runs it."""
        from review_loop import gate_shims
        self.install()
        loop = config.load_id("widgets")
        loop["seats"]["reviewer"]["profile"] = ""
        shim = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        shim.unlink()
        self.assertEqual(gate_shims.wanted(loop) & {("critic", "gate_reviewer.py")}, set())
        # Not the silent [] from the review: install writes the shim the gateway runs, and says why.
        lines = gate_shims.install(loop)
        self.assertIn(f"gate shim wrote: {shim}", lines)
        self.assertTrue(any("loop config says (no profile)/gate_reviewer.py" in line
                            for line in lines), lines)
        found = {name: (status, detail, fix) for name, status, detail, fix
                 in gate_shims.live_checks(loop)}
        status, detail, fix = found["gateway-script:widgets-review"]
        self.assertEqual(status, "mismatch", detail)
        self.assertIn("registry runs critic/gate_reviewer.py", detail)
        self.assertIn("under `seats.reviewer` in the loop config", fix)
        self.assertIn("hermes dk apply --loop widgets", fix)
        # On disk the loader refuses the blank profile by name; the named remedy clears it.
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile=""))
        with self.assertRaisesRegex(config.ConfigError, "seats.reviewer.profile is required"):
            config.load_id("widgets")
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="critic"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_route_missing_from_registry_is_named_and_repair_restores_it(self):
        self.install()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.ABSENT, check.detail)
        self.assertIn("loop config says coder/gate_fixer.py", check.detail)
        self.assertIn("registry holds no route", check.detail)
        self.assertIn("hermes dk doctor --loop widgets --repair", check.fix)
        self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIsNotNone(routes.route("widgets-fix"))
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    # -- second review of #106 -------------------------------------------------------------

    def shims(self):
        return [self.hermes / "profiles/critic/scripts/gate_reviewer.py",
                self.hermes / "profiles/coder/scripts/gate_fixer.py"]

    def test_apply_reconciles_a_route_diverged_to_a_missing_profile(self):
        """Arbiter's repro: a live pair apply is about to rebind away must not block it."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="ghost"))
        for shim in self.shims():
            shim.unlink()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("route widgets-review: profile ghost → critic", out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "critic")
        for shim in self.shims():
            self.assertTrue(shim.is_file(), shim)
        self.assertTrue(all(c.status == doctor.VERIFIED for c in self.gateway_checks().values()))
        self.assertFalse((self.hermes / "profiles/ghost").exists(), "never create a profile")

    def test_apply_rebinds_away_from_a_foreign_gate_file_but_never_writes_over_one(self):
        self.install()
        foreign = self.hermes / "profiles/arbiter/scripts/gate_reviewer.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("print('arbiter owns this')\n")
        self.edit_registry(lambda d: d["widgets-review"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "critic")
        self.assertEqual(foreign.read_text(), "print('arbiter owns this')\n")
        # A foreign file on a pair apply *would* write still refuses, before anything moves.
        mine = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        mine.write_text("print('critic owns this')\n")
        self.edit_registry(lambda d: d["widgets-review"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("not written by Diaktoros", out)
        self.assertEqual(mine.read_text(), "print('critic owns this')\n")
        self.assertEqual(routes.route("widgets-review")["profile"], "arbiter")

    def test_init_dry_run_raises_no_false_alarm_for_routes_it_would_create(self):
        from review_loop import gate_shims
        rc, out = self.run_cli(self.init_argv("acme/widgets", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("⚠️", out)
        # The exact false-alarm wording (gate_shims.divergence's missing-route line, via the
        # exported constant), not the bare substring "404": random temp-dir names in the output
        # paths can contain it (#153).
        self.assertNotIn(gate_shims.GATEWAY_404_TAIL, out)
        # #172: companion assertion — the literal must still exist in the source so a rename
        # fails loudly rather than silently disarming this test.
        gate_shims_src = ROOT / "review_loop" / "gate_shims.py"
        self.assertIn(gate_shims.GATEWAY_404_TAIL, gate_shims_src.read_text())
        # On an existing loop a real disagreement is still named.
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="arbiter"))
        rc, out = self.run_cli(self.init_argv("acme/widgets", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn("registry runs arbiter/gate_reviewer.py, loop config says critic/gate_reviewer.py",
                      out)
        self.assertNotIn(gate_shims.GATEWAY_404_TAIL, out)

    def github_with_hook(self, url: str, secret: str = "placeholder-old-hook-key", *,
                         patch_fails: bool = False, hook_id: int = 51,
                         insecure_ssl: str = "0", active: bool = True,
                         more=(), events=("pull_request_review",)) -> pathlib.Path:
        """A stateful gh stub holding one repo hook, with GitHub's worst-case PATCH semantics: the
        body's ``config`` REPLACES the hook's config wholesale, so a key left out is gone. Reads
        mask the secret as GitHub does. Every value here is a placeholder, never a real key."""
        world = self.tmp / "world.json"
        hooks = [{"id": hook_id, "active": active, "events": list(events),
                  "config": {"url": url, "content_type": "json", "insecure_ssl": insecure_ssl,
                             "secret": secret}}]
        hooks += [{"id": other[0], "active": other[2] if len(other) > 2 else True,
                   "events": list(other[3]) if len(other) > 3 else list(events),
                   "config": {"url": other[1], "content_type": "json", "insecure_ssl": "0",
                              "secret": "placeholder-other-hook-key"}}
                  for other in more]
        world.write_text(json.dumps({"hooks": hooks, "patches": 0}))
        stub = self.tmp / "gh-hooks"
        stub.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            path = sys.argv[1]
            with open({str(world)!r}) as handle:
                world = json.load(handle)
            method = os.environ.get("GH_METHOD", "GET")
            def masked(hook):
                config = dict(hook["config"])
                if config.get("secret"):
                    config["secret"] = "********"
                return {{**hook, "config": config}}
            if path.endswith("/hooks?per_page=100"):
                print(json.dumps([masked(h) for h in world["hooks"]]))
            elif "/deliveries" in path or path.endswith("/pings"):
                # #57: doctor reads each hook's latest delivery, and arm pings the hooks it arms.
                # The gateway answers every one 200, as a hook with the route's secret would get.
                hook_id = path.split("/hooks/", 1)[1].split("/", 1)[0]
                seen = world.setdefault("deliveries", {{}}).setdefault(hook_id, [
                    {{"id": 1, "event": "pull_request", "status_code": 200,
                      "delivered_at": "2026-01-01T00:00:00Z"}}])
                if path.endswith("/pings"):
                    seen.insert(0, {{"id": len(seen) + 1, "event": "ping", "status_code": 200,
                                     "delivered_at": "2026-01-01T00:%02d:00Z" % len(seen)}})
                    with open({str(world)!r}, "w") as f: json.dump(world, f)
                    print("null")
                else:
                    print(json.dumps(seen))
            elif "/hooks/" in path:
                hook = next(h for h in world["hooks"] if str(h["id"]) == path.rsplit("/", 1)[1])
                if method == "PATCH":
                    if {patch_fails!r}:
                        sys.stderr.write("HTTP 404 Not Found\\n")
                        sys.exit(1)
                    body = json.loads(sys.argv[2])
                    if "config" in body:
                        hook["config"] = dict(body["config"])      # wholesale, worst case
                    world["patches"] += 1
                    with open({str(world)!r}, "w") as handle:
                        json.dump(world, handle)
                print(json.dumps(masked(hook)))
            else:
                print("{{}}")
        """))
        stub.chmod(0o755)
        os.environ["REVIEW_LOOP_GH_STUB"] = str(stub)
        return world

    def hook_config(self, world: pathlib.Path, hook_id=None) -> dict:
        hooks = json.loads(world.read_text())["hooks"]
        return next(h for h in hooks if hook_id in (None, h["id"]))["config"]

    def logins(self):
        """Record the login each hook write is made as, while the stub still answers it."""
        from review_loop import gh
        seen = []
        real = gh.api

        def api(loop, path, method="GET", body=None, login=None):
            if method != "GET":
                seen.append((method, login))
            return real(loop, path, method, body, login)
        return seen, patch.object(gh, "api", side_effect=api)

    def test_missing_route_without_intent_record_names_a_remedy_that_works(self):
        from review_loop import route_intent
        self.install()
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        world = self.github_with_hook("https://gateway.example/p/coder/webhooks/widgets-fix")
        remedy = "hermes dk apply --loop widgets --recreate-routes"
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        for name in ("route:widgets-fix", "gateway-script:widgets-fix"):
            self.assertEqual(checks[name].status, doctor.ABSENT, checks[name].detail)
            self.assertIn(remedy, checks[name].fix)
            self.assertNotIn("re-run init", checks[name].fix)
        # Plain apply does not invent a route behind your back, and says what would.
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertIn(remedy, out)
        self.assertIsNone(routes.route("widgets-fix"))
        # The printed remedy, end to end, as the admin login (the reader is usually read-only).
        seen, recording = self.logins()
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"), recording:
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes",
                                    "--admin-token", "admin-acct"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [("PATCH", "admin-acct")])
        entry = routes.route("widgets-fix")
        self.assertEqual((entry["profile"], entry["script"], entry["secret"]),
                         ("coder", "gate_fixer.py", "placeholder-recreated-key"))
        self.assertIn("hook 51", out)
        self.assertNotIn("placeholder-recreated-key", out, "never print a secret")
        self.assertEqual(self.hook_config(world),
                         {"url": "https://gateway.example/p/coder/webhooks/widgets-fix",
                          "content_type": "json", "insecure_ssl": "0",
                          "secret": "placeholder-recreated-key"})
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        for name in ("route:widgets-fix", "gateway-script:widgets-fix"):
            self.assertEqual(checks[name].status, doctor.VERIFIED, checks[name].detail)
        self.assertEqual(route_intent.load(config.load_id("widgets"))["widgets-fix"]["secret"],
                         entry["secret"])

    def test_recreate_routes_refused_hook_write_rolls_back_and_names_the_scope(self):
        from review_loop import route_intent
        self.install()
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        world = self.github_with_hook("https://gateway.example/p/coder/webhooks/widgets-fix",
                                      patch_fails=True)
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 2, out)
        self.assertIsNone(routes.route("widgets-fix"), "the recreated route is taken back out")
        self.assertNotIn("widgets-fix", route_intent.load(config.load_id("widgets")) or {})
        self.assertIn("--admin-token <login>", out)
        self.assertIn("admin:repo_hook", out)
        self.assertNotIn("placeholder-recreated-key", out, "never print a secret")
        self.assertEqual(self.hook_config(world)["secret"], "placeholder-old-hook-key")

    def test_set_host_then_apply_moves_the_loops_hooks_to_the_new_origin(self):
        """#112: apply rewrites the routes' recorded origin after `set --host`; the repo hooks that
        post to the old origin move with them, through #106's hook-move path."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        old_review = "https://gateway.example/p/critic/webhooks/widgets-review"
        old_fix = "https://gateway.example/p/coder/webhooks/widgets-fix"
        world = self.github_with_hook(old_review, secret=REVIEW_KEY, hook_id=41,
                                      more=[(51, old_fix)],
                                      events=("pull_request", "pull_request_review"))
        rc, out = self.run_cli(["set", "--loop", "widgets", "--host", "https://moved.example"])
        self.assertEqual(rc, 0, out)
        self.assertIn("next: `hermes dk apply --loop widgets`", out)
        new_review = "https://moved.example/p/critic/webhooks/widgets-review"
        new_fix = "https://moved.example/p/coder/webhooks/widgets-fix"

        rc, dry = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertIn(f"hook 41 would move → {new_review}", dry)
        self.assertIn(f"hook 51 would move → {new_fix}", dry)
        self.assertEqual(json.loads(world.read_text())["patches"], 0, "a dry run wrote")

        seen, recording = self.logins()
        with recording:
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--admin-token", "admin-acct"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hook 41 → {new_review}", out)
        self.assertIn(f"hook 51 → {new_fix}", out)
        self.assertEqual(seen, [("PATCH", "admin-acct"), ("PATCH", "admin-acct")])
        self.assertEqual(self.hook_config(world, 41),
                         {"url": new_review, "content_type": "json", "insecure_ssl": "0",
                          "secret": REVIEW_KEY})
        self.assertEqual(self.hook_config(world, 51)["url"], new_fix)
        self.assertNotIn(REVIEW_KEY, out, "never print a secret")
        for name in ("widgets-review", "widgets-fix"):
            self.assertEqual(routes.route(name)["host"], "https://moved.example")
        hooks = [c for c in doctor.check_hooks(config.load_id("widgets"), offline=False)]
        self.assertTrue(hooks and all(c.status == doctor.VERIFIED for c in hooks),
                        [(c.name, c.status, c.detail) for c in hooks])
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual((rc, "already matches" in out), (0, True), out)

    def test_a_hook_url_move_keeps_the_hook_secret_under_wholesale_patch(self):
        """apply moving a hook to the seat's new profile sends the whole config, secret included."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        world = self.github_with_hook("https://gateway.example/p/critic/webhooks/widgets-review",
                                      secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 41 → https://gateway.example/p/arbiter/webhooks/widgets-review", out)
        self.assertEqual(self.hook_config(world),
                         {"url": "https://gateway.example/p/arbiter/webhooks/widgets-review",
                          "content_type": "json", "insecure_ssl": "0",
                          "secret": REVIEW_KEY})
        self.assertNotIn(REVIEW_KEY, out, "never print a secret")

    def test_apply_moves_hooks_as_the_admin_login(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        self.github_with_hook("https://gateway.example/p/critic/webhooks/widgets-review",
                              secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        seen, recording = self.logins()
        with recording:
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--admin-token", "admin-acct"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [("PATCH", "admin-acct")])

    def test_a_hook_move_and_a_re_key_keep_the_hooks_own_insecure_ssl(self):
        """The operator's TLS choice on a hook is theirs: a move or re-key never normalizes it."""
        from review_loop import route_intent
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        world = self.github_with_hook("https://gateway.example/p/critic/webhooks/widgets-review",
                                      secret=REVIEW_KEY, hook_id=41, insecure_ssl="1")
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world)["insecure_ssl"], "1")
        # And the re-key of a recreated route.
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        world = self.github_with_hook("https://gateway.example/p/coder/webhooks/widgets-fix",
                                      insecure_ssl="1")
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world)["insecure_ssl"], "1")

    OLD_FIX = "https://gateway.example/p/critic/webhooks/widgets-fix"      # a profile it left
    NEW_FIX = "https://gateway.example/p/coder/webhooks/widgets-fix"

    def lose_fix_route(self):
        from review_loop import route_intent
        self.install()
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))

    def test_recreate_moves_a_hook_left_at_the_routes_previous_url(self):
        self.lose_fix_route()
        world = self.github_with_hook(self.OLD_FIX, hook_id=101)
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 101", out)
        self.assertNotIn("no repo hook points at it", out)
        self.assertEqual(self.hook_config(world, 101),
                         {"url": self.NEW_FIX, "content_type": "json", "insecure_ssl": "0",
                          "secret": "placeholder-recreated-key"})

    def test_recreate_with_hooks_at_old_and_new_url_keeps_one_and_names_the_other(self):
        self.lose_fix_route()
        world = self.github_with_hook(self.OLD_FIX, hook_id=101, more=[(102, self.NEW_FIX)])
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.hook_config(world, 102)["secret"], "placeholder-recreated-key")
        self.assertEqual(self.hook_config(world, 101)["url"], self.OLD_FIX, "never a duplicate")
        self.assertIn("hook 101", out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/101", out)
        self.assertNotIn("placeholder-recreated-key", out)

    def test_a_hook_move_never_duplicates_a_hook_already_at_the_target(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        old = "https://gateway.example/p/critic/webhooks/widgets-review"
        new = "https://gateway.example/p/arbiter/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=41, more=[(42, new)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "arbiter")
        self.assertEqual(self.hook_config(world, 41)["url"], old, "the redundant hook is not moved")
        self.assertEqual(self.hook_config(world, 42)["url"], new)
        self.assertIn("hook 42 (active) is the one kept", out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/41", out)

    def hooks_on(self, world, url):
        return sorted((h["id"], h["active"]) for h in json.loads(world.read_text())["hooks"]
                      if h["config"]["url"] == url)

    def test_a_paused_hook_at_the_target_never_beats_the_live_one(self):
        """Arbiter's probe: 41 live on the old URL, 42 paused on the target. The route must end with
        its live hook on its URL, and the paused one named — never the other way round."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        old = "https://gateway.example/p/critic/webhooks/widgets-review"
        new = "https://gateway.example/p/arbiter/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=41,
                                      more=[(42, new, False)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertEqual([h for h in self.hooks_on(world, new) if h[1]], [(41, True)],
                         "exactly one ACTIVE hook on the route URL: the live one")
        self.assertEqual(self.hook_config(world, 41)["secret"], REVIEW_KEY)
        self.assertIn("hook 42 (paused)", out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/42", out)
        self.assertNotIn("hooks/41`", out)

    def test_when_every_hook_is_on_the_old_url_exactly_one_moves(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        old = "https://gateway.example/p/critic/webhooks/widgets-review"
        new = "https://gateway.example/p/arbiter/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=43, more=[(41, old)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.hooks_on(world, new), [(41, True)], "the lowest id moves, alone")
        self.assertEqual(self.hooks_on(world, old), [(43, True)])
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/43", out)

    def test_plain_apply_names_a_duplicate_already_on_the_current_url(self):
        self.install()
        url = "https://gateway.example/p/critic/webhooks/widgets-review"
        world = self.github_with_hook(url, hook_id=43, more=[(41, url)])
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertIn("already matches the plugin settings", out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/43", out)
        self.assertEqual(self.hooks_on(world, url), [(41, True), (43, True)], "reads only")
        self.assertEqual(json.loads(world.read_text())["patches"], 0)
        # One hook per route: nothing to say, rc 0.
        self.github_with_hook(url, hook_id=41)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)

    # -- a trailing slash: the gateway 404s it, so every surface must say so (review of 1f1fe28)

    REVIEW_URL = "https://gateway.example/p/critic/webhooks/widgets-review"

    def hook_check(self):
        loop = config.load_id("widgets")
        return {c.name: c for c in doctor.check_hooks(loop, offline=False)}["hook:widgets-review"]

    def test_doctor_does_not_verify_a_hook_whose_url_has_a_trailing_slash(self):
        self.install()
        self.github_with_hook(self.REVIEW_URL + "/", hook_id=41, events=("pull_request",))
        check = self.hook_check()
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("trailing slash", check.detail)
        self.assertIn("hermes dk apply --loop widgets", check.fix)

    def test_plain_apply_repoints_a_trailing_slash_hook_and_doctor_then_verifies(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        world = self.github_with_hook(self.REVIEW_URL + "/", secret=REVIEW_KEY, hook_id=41,
                                      events=("pull_request",))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hook 41 → {self.REVIEW_URL}", out)
        self.assertEqual(self.hook_config(world, 41),
                         {"url": self.REVIEW_URL, "content_type": "json", "insecure_ssl": "0",
                          "secret": REVIEW_KEY})
        self.assertEqual(self.hook_check().status, doctor.VERIFIED, self.hook_check().detail)

    def test_plain_apply_names_a_trailing_slash_duplicate(self):
        self.install()
        world = self.github_with_hook(self.REVIEW_URL, hook_id=41,
                                      more=[(43, self.REVIEW_URL + "/")])
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/43", out)
        self.assertEqual(json.loads(world.read_text())["patches"], 0)

    def test_a_hook_move_accepts_the_old_url_with_a_trailing_slash(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        new = "https://gateway.example/p/arbiter/webhooks/widgets-review"
        world = self.github_with_hook(self.REVIEW_URL + "/", secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="arbiter"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world, 41)["url"], new)

    FIX_URL = "https://gateway.example/p/coder/webhooks/widgets-fix"

    def test_a_trailing_slash_hook_is_not_armed_anywhere(self):
        """explain/watchdog (gate.hooks_read) and `arm` agree with doctor: the gateway 404s it."""
        from review_loop import gate
        self.install()
        self.github_with_hook(self.REVIEW_URL + "/", hook_id=41, events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        armed, missing = gate.hooks_read(config.load_id("widgets"))
        self.assertEqual((armed, missing), (False, "reviewer"))
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertIn("hook 41", out)
        self.assertIn("does not route", out)
        self.assertIn("hermes dk apply --hooks --loop widgets", out)
        # Exact URLs are armed, and `arm` says so with rc 0.
        self.github_with_hook(self.REVIEW_URL, hook_id=41, events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        self.assertEqual(gate.hooks_read(config.load_id("widgets")), (True, ""))
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)

    def test_a_query_string_hook_is_served_so_every_surface_calls_it_armed(self):
        """The gateway routes on the exact PATH (aiohttp): `?x=1` reaches the handler, a trailing
        slash is a 404. So a query-string hook is armed and correct everywhere — explain/watchdog,
        arm, doctor, apply — while the slashed spelling stays refused."""
        from review_loop import gate
        self.install()
        world = self.github_with_hook(self.REVIEW_URL + "?x=1", hook_id=41,
                                      events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        loop = config.load_id("widgets")
        self.assertEqual(gate.hooks_read(loop), (True, ""))
        self.assertIs(gate.hooks_armed(loop), True, "the watchdog must not park a working seat")
        rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("⚠️", out)
        self.assertEqual(self.hook_check().status, doctor.VERIFIED, self.hook_check().detail)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(world.read_text())["patches"], 0, "a working hook is untouched")
        # Scheme and host compare case-insensitively; the path never does.
        self.github_with_hook("HTTPS://Gateway.Example/p/critic/webhooks/widgets-review",
                              hook_id=41, events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        self.assertEqual(gate.hooks_read(loop), (True, ""))
        self.github_with_hook("https://gateway.example/p/critic/webhooks/Widgets-Review",
                              hook_id=41, events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        self.assertEqual(gate.hooks_read(loop), (False, "reviewer"))
        # And the trailing slash is still a 404, with or without a query.
        self.github_with_hook(self.REVIEW_URL + "/?x=1", hook_id=41, events=("pull_request",),
                              more=[(42, self.FIX_URL, True, ("pull_request_review",))])
        self.assertEqual(gate.hooks_read(loop), (False, "reviewer"))

    def test_recreate_re_keys_a_query_string_hook_in_place(self):
        self.lose_fix_route()
        world = self.github_with_hook(self.NEW_FIX + "?x=1", hook_id=102)
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world, 102)["url"], self.NEW_FIX + "?x=1")
        self.assertEqual(self.hook_config(world, 102)["secret"], "placeholder-recreated-key")

    def test_a_repair_names_the_fix_for_its_cause_not_always_the_token(self):
        """A route with no secret is not a token problem: no --admin-token remedy for it."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=""))
        self.github_with_hook(self.REVIEW_URL + "/", hook_id=41, events=("pull_request",))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("has no secret", out)
        self.assertNotIn("--admin-token", out)
        self.assertIn("hermes dk doctor --loop widgets", out)
        # A refused write is: that one does name the token scope.
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        self.github_with_hook(self.REVIEW_URL + "/", hook_id=41, events=("pull_request",),
                              patch_fails=True)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("--admin-token <login>", out)
        self.assertIn("admin:repo_hook", out)

    def test_recreate_routes_refuses_when_the_hook_listing_cannot_be_read(self):
        from review_loop import route_intent
        self.install()
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        broken = self.tmp / "gh-broken"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        os.environ["REVIEW_LOOP_GH_STUB"] = str(broken)
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 2, out)
        self.assertIn("hook listing", out)
        self.assertIsNone(routes.route("widgets-fix"))

    def test_uninstall_keeps_shims_another_loop_needs(self):
        self.install("acme/widgets")
        self.install("acme/gizmos", "--id", "gizmos", "--fixer-profile", "arbiter")
        critic = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        coder = self.hermes / "profiles/coder/scripts/gate_fixer.py"
        arbiter = self.hermes / "profiles/arbiter/scripts/gate_fixer.py"
        rc, out = self.run_cli(["uninstall", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(critic.is_file(), "gizmos still routes its reviewer through critic")
        self.assertFalse(coder.exists())
        self.assertIn(f"gate shim removed: {coder}", out)
        critic.write_text("print('hand edited')\n")      # not ours any more: never removed
        rc, out = self.run_cli(["uninstall", "--loop", "gizmos"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(arbiter.exists())
        self.assertEqual(critic.read_text(), "print('hand edited')\n")

    def test_watchdog_heal_restores_a_deleted_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/coder/scripts/gate_fixer.py"
        shim.unlink()
        lines = gate_shims.heal(config.load_id("widgets"))
        self.assertTrue(shim.is_file())
        self.assertIn("restored 1 gate shim", lines[0])
        self.assertEqual(gate_shims.heal(config.load_id("widgets")), [])
        # The heal follows the registry, not the config: no route, nothing for the gateway to run.
        routes.remove_route("widgets-fix")
        shim.unlink()
        self.assertEqual(gate_shims.heal(config.load_id("widgets")), [])
        self.assertFalse(shim.exists())

    def test_set_fails_loudly_when_the_observer_shim_cannot_be_written(self):
        self.install()
        foreign = self.hermes / "profiles/arbiter/scripts/observe.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("print('arbiter owns this')\n")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--observer-profile", "arbiter",
                                "--observer-deliver", "telegram"])
        self.assertEqual(rc, 1, out)
        self.assertIn("gate shim install FAILED", out)
        self.assertIn("hermes dk apply --loop widgets", out)
        self.assertEqual(foreign.read_text(), "print('arbiter owns this')\n")

    def test_heal_says_what_the_gateway_does_for_each_refusal(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/critic/scripts/gate_reviewer.py"
        shim.write_text("print('someone else')\n")
        lines = "\n".join(gate_shims.heal(config.load_id("widgets")))
        self.assertIn("runs that instead of the gate", lines)
        self.assertNotIn("drops", lines)
        self.assertEqual(shim.read_text(), "print('someone else')\n")
        shim.unlink()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="ghost"))
        lines = "\n".join(gate_shims.heal(config.load_id("widgets")))
        self.assertIn("the gateway drops", lines)
        self.assertIn("ghost", lines)
        self.assertNotIn("instead of the gate", lines)

    def test_gate_names_agree(self):
        from review_loop import gate_shims
        self.assertEqual(gate_shims.GATE_SCRIPT, cli.GATE_SCRIPT)


class ObserverApply(Base):
    """Issue #107: `apply` must check the observer route against the profile `init` wrote."""

    def test_every_path_names_the_profile_init_wrote(self):
        self.observer_install()
        loop = config.load_id("widgets")
        self.assertEqual(routes.route("widgets-observe")["profile"], "arbiter")
        self.assertEqual(config.seat_profile(loop, "observer"), "arbiter")
        from review_loop import gate_shims, observer
        self.assertIn(("arbiter", "observe.py"), gate_shims.wanted(loop))
        self.assertEqual(observer.route_contract(loop)["profile"], "arbiter")
        self.assertEqual(cli._route_binds(loop, set(cli._routes_of(loop))), {})

    def test_no_observer_has_no_observer_profile(self):
        self.install()
        self.assertEqual(config.seat_profile(config.load_id("widgets"), "observer"), "")

    def test_apply_on_an_observer_loop_is_a_no_op(self):
        self.observer_install()
        before = self.loop_routes()
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("readback does not match", out)
        self.assertIn("already matches the plugin settings", out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn("already matches the plugin settings", out)
        self.assertEqual(self.loop_routes(), before)

    def test_apply_rebinds_a_drifted_observer_route_to_its_profile(self):
        self.observer_install()
        entry = routes.route("widgets-observe")
        routes.new_route("widgets-observe", profile="coder", prompt=entry["prompt"],
                         events=["pull_request"], script="observe.py",
                         deliver=entry.get("deliver", "telegram"), deliver_only=True,
                         host="https://gateway.example")
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn("route widgets-observe: profile coder → arbiter", out)
        self.assertIn("route widgets-observe rebound → profile arbiter", out)
        self.assertEqual(routes.route("widgets-observe")["profile"], "arbiter")
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual((rc, "already matches the plugin settings" in out), (0, True), out)


class ObserverStatusDoctor(Base):
    """The observer route gets the same status line and doctor route/intent checks as a seat."""

    def route_check(self):
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        return checks.get("route:widgets-observe")

    def drift(self, **changes):
        entry = dict(routes.route("widgets-observe"))
        kwargs = {"profile": entry["profile"], "prompt": entry["prompt"],
                  "events": entry["events"], "script": entry["script"],
                  "deliver": entry["deliver"], "deliver_only": entry.get("deliver_only", False),
                  "host": entry.get("host")}
        kwargs.update(changes)
        routes.new_route("widgets-observe", **kwargs)

    def status(self):
        rc, out = self.run_cli(["status", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        return next(line for line in out.splitlines() if line.strip().startswith("routes:"))

    def test_doctor_verifies_a_healthy_observer_route_against_its_intent_record(self):
        self.observer_install()
        check = self.route_check()
        self.assertIsNotNone(check, "doctor has no route check for the observer")
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("arbiter", check.detail)
        self.assertIn("matches intent record", check.detail)

    def test_doctor_flags_a_drifted_observer_profile_and_apply_clears_it(self):
        self.observer_install()
        self.drift(profile="coder")
        check = self.route_check()
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("'coder'", check.detail)
        self.assertIn("'arbiter'", check.detail)
        self.assertIn("intent record", check.detail)
        self.assertTrue(check.fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.route_check().status, doctor.VERIFIED)

    def test_doctor_names_the_fix_without_an_intent_record(self):
        from review_loop import route_intent
        self.observer_install()
        route_intent.path(config.load_id("widgets")).unlink()
        self.drift(profile="coder")
        check = self.route_check()
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("hermes dk apply --loop widgets", check.fix)
        self.drift(profile="arbiter", deliver_only=False)
        check = self.route_check()
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("deliver_only", check.detail)
        self.assertTrue(check.fix)

    def test_doctor_flags_a_missing_observer_route_and_repair_restores_it(self):
        self.observer_install()
        routes.remove_route("widgets-observe")
        check = self.route_check()
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn("hermes dk doctor --loop widgets --repair", check.fix)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline", "--repair"])
        self.assertIn("widgets-observe", out)
        self.assertEqual(self.route_check().status, doctor.VERIFIED)

    def test_no_observer_no_observer_route_check(self):
        self.install()
        self.assertIsNone(self.route_check())

    def test_status_lists_the_observer_route(self):
        self.observer_install()
        self.assertIn("observer widgets-observe → arbiter (ok)", self.status())
        self.drift(profile="coder")
        self.assertIn("observer widgets-observe → coder, not arbiter: MISMATCH — "
                      "hermes dk apply --loop widgets", self.status())
        routes.remove_route("widgets-observe")
        line = self.status()
        self.assertIn("observer widgets-observe: not installed — "
                      "hermes dk doctor --loop widgets --repair", line)

    def test_status_says_when_the_observer_is_muted(self):
        self.observer_install()
        rc, out = self.run_cli(["set", "--loop", "widgets", "--observer-mute"])
        self.assertEqual(rc, 0, out)
        self.assertIn("observer widgets-observe → arbiter (ok, muted)", self.status())

    # -- review of #112: doctor and the feed read a route's profile the way the gateway does ----

    def forget_intent(self):
        from review_loop import route_intent
        route_intent.path(config.load_id("widgets")).unlink()

    def edit_registry(self, name, mutate):
        path = pathlib.Path(self.env["REVIEW_LOOP_SUBS"])
        data = json.loads(path.read_text())
        mutate(data[name])
        path.write_text(json.dumps(data, indent=2))

    def feed_target(self):
        from review_loop import observer
        return observer._target(config.load_id("widgets"))

    def follow(self, fix):
        """Run the first `hermes dk …` command a fix line names, as printed."""
        import re
        import shlex
        commands = re.findall(r"`hermes dk ([^`]+)`", fix)
        self.assertTrue(commands, f"no runnable command in: {fix}")
        argv = shlex.split(commands[0])
        if argv[0] == "doctor":
            # Its --repair write is the remedy; --offline only keeps the read-only probes that
            # follow it off the network. Its exit code is the whole preflight (this fixture has
            # no cron job), so success is the repair line instead.
            rc, out = self.run_cli([*argv, "--offline"])
            self.assertIn("restored", out)
            return out
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 0, out)
        return out

    def observer_check(self):
        name = config.load_id("widgets")["observer"]["route"]
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        return checks.get(f"route:{name}")

    def test_a_route_without_a_profile_key_serves_default_for_doctor_and_feed(self):
        # The gateway binds a route with no `profile` key to default (_route_allows_profile).
        self.install("acme/widgets", "--observer-profile", "default")
        self.forget_intent()
        self.edit_registry("widgets-observe", lambda e: e.pop("profile"))
        check = self.route_check()
        target = self.feed_target()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIsNotNone(target, "the feed refuses a route the gateway serves as default")
        self.assertEqual(target[0], "https://gateway.example/webhooks/widgets-observe")
        self.assertIn("observer widgets-observe → default (ok)", self.status())

    def test_a_blank_or_null_profile_fails_closed_in_doctor_and_feed(self):
        # The gateway refuses every request for an explicit null/blank/non-string profile.
        self.install("acme/widgets", "--observer-profile", "default")
        self.forget_intent()
        for bad in ("", "  ", None, 7):
            self.edit_registry("widgets-observe", lambda e: e.update(profile=bad))
            check = self.route_check()
            self.assertEqual(check.status, doctor.MISMATCH, f"{bad!r}: {check.detail}")
            self.assertIsNone(self.feed_target(), repr(bad))
            self.assertIn("MISMATCH", self.status())
        self.follow(check.fix)
        self.assertEqual(self.route_check().status, doctor.VERIFIED)
        self.assertIsNotNone(self.feed_target())

    def test_seat_route_checks_read_the_profile_the_same_way(self):
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "default")
        self.forget_intent()
        self.edit_registry("widgets-breach", lambda e: e.pop("profile"))
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        self.assertEqual(checks["route:widgets-breach"].status, doctor.VERIFIED,
                         checks["route:widgets-breach"].detail)
        self.edit_registry("widgets-breach", lambda e: e.update(profile=""))
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        self.assertEqual(checks["route:widgets-breach"].status, doctor.MISMATCH)

    def test_route_profile_matches_the_gateway_rule(self):
        cases = [{}, {"profile": "default"}, {"profile": "arbiter"}, {"profile": " arbiter "},
                 {"profile": ""}, {"profile": "  "}, {"profile": None}, {"profile": 7}]
        expected = ["default", "default", "arbiter", "arbiter", None, None, None, None]
        self.assertEqual([routes.route_profile(c) for c in cases], expected)
        source = SOURCE / "gateway" / "platforms" / "webhook.py"
        if not source.exists():
            skip_or_fail(self, f"no Hermes gateway source at {source}")
        import ast
        tree = ast.parse(source.read_text())
        func = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                    and node.name == "_route_allows_profile")
        func.decorator_list = []
        func.args.args[1].annotation = None
        func.returns = None
        scope: dict = {}
        exec(compile(ast.Module([func], []), str(source), "exec"), scope)
        allows = scope["_route_allows_profile"]
        for case, ours in zip(cases, expected):
            for requested in (None, "arbiter"):
                self.assertEqual(allows(case, requested),
                                 ours is not None and ours == (requested or "default"),
                                 f"{case} for /p/{requested}")

    def test_deliver_drift_is_healed_by_the_repair_doctor_names(self):
        self.observer_install()
        self.edit_registry("widgets-observe", lambda e: e.update(deliver="discord",
                                                                 deliver_extra={"chat": "x"}))
        check = self.route_check()
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIsNone(self.feed_target())
        self.assertIn("--repair", check.fix)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline", "--repair"])
        self.assertIn("widgets-observe: had changed deliver, deliver_extra", out)
        self.assertEqual(self.route_check().status, doctor.VERIFIED)
        self.assertIsNotNone(self.feed_target())

    def test_deliver_drift_without_a_record_names_a_remedy_that_works(self):
        self.observer_install()
        self.forget_intent()
        self.edit_registry("widgets-observe", lambda e: e.update(deliver="discord"))
        check = self.route_check()
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertNotIn("--repair", check.fix)
        self.follow(check.fix)
        check = self.observer_check()
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIsNotNone(self.feed_target())

    def test_a_missing_route_without_a_record_names_a_remedy_that_works(self):
        self.observer_install()
        self.forget_intent()
        routes.remove_route("widgets-observe")
        check = self.route_check()
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertNotIn("--repair", check.fix)
        line = self.status()
        self.assertIn("observer widgets-observe: not installed — ", line)
        self.assertNotIn("--repair", line)
        import re
        command = re.search(r"not installed — (hermes dk [^·]+?)(?: ·|$)", line).group(1)
        self.follow(f"`{command.strip()}`")
        self.assertEqual(self.observer_check().status, doctor.VERIFIED)
        self.assertIsNotNone(self.feed_target())
        self.assertIn(" (ok)", self.status())

    # -- debt from the approval of #112 ---------------------------------------------------------

    def test_a_route_another_writer_took_over_gets_an_honest_remedy(self):
        self.install()
        self.edit_registry("widgets-review", lambda e: e.update(script="someone_elses.py"))
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        check = checks["gateway-script:widgets-review"]
        self.assertTrue(check.failed, check.detail)
        self.assertIn("not a Diaktoros gate", check.detail)
        # Repair refuses a route something else holds: it may only come second, after the
        # operator moves that entry out of the way.
        self.assertTrue(check.fix.startswith("remove or rename that entry"), check.fix)
        # The route check says the same: its intent-record drift must not promise a repair.
        route_check = checks["route:widgets-review"]
        self.assertTrue(route_check.fix.startswith("remove or rename that entry"), route_check.fix)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline", "--repair"])
        self.assertIn("NOT restored", out)
        routes.remove_route("widgets-review")
        self.follow(check.fix)
        checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)}
        self.assertEqual(checks["gateway-script:widgets-review"].status, doctor.VERIFIED)
        self.assertEqual(checks["route:widgets-review"].status, doctor.VERIFIED)

    def test_apply_dry_run_reports_the_divergence_the_real_apply_reports(self):
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "arbiter")
        routes.remove_route("widgets-breach")
        settings = {"cap": 5}              # another change in flight
        rc_dry, dry = self.run_cli(["apply", "--loop", "widgets", "--dry-run"], settings=settings)
        self.assertIn("cap: 3 → 5", dry)
        self.assertEqual(config.load_id("widgets")["cap"], 3, "a dry run wrote the config")
        rc, real = self.run_cli(["apply", "--loop", "widgets"], settings=settings)
        self.assertEqual(rc, 1, real)
        warned = [line for line in real.splitlines() if "⚠️ route" in line]
        self.assertTrue(warned, real)
        self.assertEqual([line for line in dry.splitlines() if "⚠️ route" in line], warned)
        self.assertIn("apply would exit 1", dry)
        self.assertEqual(rc_dry, 1, dry)

    # One mutation per registry field the gateway or the feed reads. Each returns the entry.
    MUTATIONS = {
        "profile key removed": lambda e: e.pop("profile"),
        "profile null": lambda e: e.update(profile=None),
        "profile blank": lambda e: e.update(profile=""),
        "profile whitespace": lambda e: e.update(profile="   "),
        "profile padded": lambda e: e.update(profile=" arbiter "),
        "profile not a string": lambda e: e.update(profile=7),
        "another profile": lambda e: e.update(profile="coder"),
        "deliver changed": lambda e: e.update(deliver="discord"),
        "deliver_extra added": lambda e: e.update(deliver_extra={"chat_id": "elsewhere"}),
        "deliver_only false": lambda e: e.update(deliver_only=False),
        "deliver_only removed": lambda e: e.pop("deliver_only"),
        "prompt changed": lambda e: e.update(prompt="do something else"),
        "script changed": lambda e: e.update(script="gate_reviewer.py"),
        "events changed": lambda e: e.update(events=["push"]),
        "secret removed": lambda e: e.pop("secret"),
        "secret blank": lambda e: e.update(secret=""),
        "host removed": lambda e: e.pop("host"),
        "host elsewhere": lambda e: e.update(host="https://elsewhere.example"),
        "enabled false": lambda e: e.update(enabled=False),
        "enabled true": lambda e: e.update(enabled=True),
        "route erased": None,
    }

    def sweep(self, with_record: bool):
        from review_loop import route_intent
        self.observer_install()
        if not with_record:
            self.forget_intent()
        subs = pathlib.Path(self.env["REVIEW_LOOP_SUBS"])
        pristine = subs.read_text()
        results = {}
        for label, mutate in self.MUTATIONS.items():
            subs.write_text(pristine)
            if mutate is None:
                routes.remove_route("widgets-observe")
            else:
                self.edit_registry("widgets-observe", mutate)
            check = self.route_check()
            results[label] = (check.status, self.feed_target() is not None, check.fix)
        return results

    def test_doctor_and_the_feed_agree_on_every_mutation_without_a_record(self):
        for label, (status, delivers, fix) in self.sweep(with_record=False).items():
            with self.subTest(label):
                self.assertEqual(status == doctor.VERIFIED, delivers,
                                 f"doctor {status}, feed {'delivers' if delivers else 'refuses'}")
                if status != doctor.VERIFIED:
                    self.assertIn("`hermes dk ", fix)

    def test_with_a_record_doctor_is_never_green_over_a_refusing_feed(self):
        # The intent record makes doctor stricter on purpose: any drift from what the plugin
        # wrote is red (and repaired), even a field the feed can live with.
        for label, (status, delivers, fix) in self.sweep(with_record=True).items():
            with self.subTest(label):
                if status == doctor.VERIFIED:
                    self.assertTrue(delivers, "green over a feed that refuses")
                else:
                    self.assertIn("`hermes dk ", fix)

    def test_a_disabled_route_is_red_and_the_named_remedy_reenables_it(self):
        # The gateway answers 403 for an explicit `enabled: false` (webhook.py), so neither
        # doctor nor the feed may treat that route as a way to deliver.
        for with_record in (True, False):
            with self.subTest(with_record=with_record):
                self.setUp()
                self.observer_install()
                if not with_record:
                    self.forget_intent()
                self.edit_registry("widgets-observe", lambda e: e.update(enabled=False))
                check = self.route_check()
                self.assertEqual(check.status, doctor.MISMATCH, check.detail)
                self.assertIn("enabled", check.detail)
                self.assertIsNone(self.feed_target())
                self.follow(check.fix)
                self.assertEqual(self.observer_check().status, doctor.VERIFIED)
                self.assertIsNotNone(self.feed_target())

    def test_a_disabled_seat_route_is_red_and_the_named_remedy_reenables_it(self):
        # `enabled: false` makes the gateway answer 403 to every event: a seat that is never woken.
        for role, name, with_record in (("reviewer", "widgets-review", True),
                                        ("reviewer", "widgets-review", False),
                                        ("fixer", "widgets-fix", False),
                                        ("adjudicator", "widgets-breach", True),
                                        ("adjudicator", "widgets-breach", False)):
            with self.subTest(route=name, with_record=with_record):
                self.setUp()
                self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                             "--adjudicator-profile", "arbiter")
                if not with_record:
                    self.forget_intent()
                self.edit_registry(name, lambda e: e.update(enabled=False))
                checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"),
                                                               offline=True)}
                check = checks[f"route:{name}"]
                self.assertEqual(check.status, doctor.MISMATCH, check.detail)
                self.assertIn("enabled", check.detail)
                self.assertIn("--repair" if with_record else "apply --loop widgets", check.fix)
                if not with_record:
                    rc, dry = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
                    self.assertIn(f"route {name}: disabled", dry)
                    self.assertFalse(routes.route(name).get("enabled", True), "dry run wrote")
                self.follow(check.fix)
                self.assertNotIn("enabled", routes.route(name))
                checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"),
                                                               offline=True)}
                self.assertEqual(checks[f"route:{name}"].status, doctor.VERIFIED,
                                 checks[f"route:{name}"].detail)

    def test_the_watchdog_heal_reenables_a_disabled_seat_route(self):
        from review_loop import route_intent
        self.install()
        self.edit_registry("widgets-fix", lambda e: e.update(enabled=False))
        lines = route_intent.heal(config.load_id("widgets"))
        self.assertTrue(any("widgets-fix: had changed enabled" in line for line in lines), lines)
        self.assertNotIn("enabled", routes.route("widgets-fix"))

    # -- third review of #112: apply sees contract drift; a refused profile is not "none" --------

    def apply_until_quiet(self):
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertNotIn("already matches the plugin settings", out)
        return rc, out

    def test_apply_repairs_observer_contract_drift_it_can_prove_is_ours(self):
        drifts = {"deliver_only removed": lambda e: e.pop("deliver_only"),
                  "deliver changed": lambda e: e.update(deliver="discord"),
                  "deliver_extra added": lambda e: e.update(deliver_extra={"chat_id": "x"}),
                  "events changed": lambda e: e.update(events=["push"]),
                  "disabled": lambda e: e.update(enabled=False)}
        for label, mutate in drifts.items():
            for with_record in (True, False):
                with self.subTest(label, with_record=with_record):
                    self.setUp()
                    self.observer_install()
                    if not with_record:
                        self.forget_intent()
                    self.edit_registry("widgets-observe", mutate)
                    self.assertIsNone(self.feed_target())
                    rc, dry = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
                    self.assertIn("route widgets-observe:", dry)
                    self.assertIsNone(self.feed_target(), "a dry run wrote")
                    rc, out = self.apply_until_quiet()
                    self.assertEqual(rc, 0, out)
                    self.assertIn("route widgets-observe:", out)
                    self.assertEqual(self.route_check().status, doctor.VERIFIED)
                    self.assertIsNotNone(self.feed_target())
                    rc, out = self.run_cli(["apply", "--loop", "widgets"])
                    self.assertEqual((rc, "already matches" in out), (0, True), out)

    def test_apply_repairs_seat_contract_drift(self):
        drifts = {"events changed": lambda e: e.update(events=["push"]),
                  "deliver_only set": lambda e: e.update(deliver_only=True),
                  "disabled": lambda e: e.update(enabled=False)}
        for label, mutate in drifts.items():
            with self.subTest(label):
                self.setUp()
                self.install()
                self.forget_intent()
                self.edit_registry("widgets-fix", mutate)
                checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"),
                                                               offline=True)}
                self.assertTrue(checks["route:widgets-fix"].failed, label)
                rc, out = self.apply_until_quiet()
                self.assertEqual(rc, 0, out)
                self.assertIn("route widgets-fix:", out)
                checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"),
                                                               offline=True)}
                self.assertEqual(checks["route:widgets-fix"].status, doctor.VERIFIED,
                                 checks["route:widgets-fix"].detail)

    def test_apply_reports_drift_it_cannot_prove_is_ours(self):
        # A changed prompt breaks the ownership proof, so apply must not rewrite the route — but
        # it must not call the loop clean either.
        for role, name in (("fixer", "widgets-fix"), ("observer", "widgets-observe")):
            with self.subTest(name):
                self.setUp()
                self.observer_install()
                self.edit_registry(name, lambda e: e.update(prompt="something else"))
                for with_record in (True, False):
                    if not with_record:
                        self.forget_intent()
                    checks = {c.name: c for c in doctor.check_loop(config.load_id("widgets"),
                                                                   offline=True)}
                    self.assertTrue(checks[f"route:{name}"].failed,
                                    f"doctor green over a foreign prompt (record: {with_record})")
                rc, out = self.apply_until_quiet()
                self.assertEqual(rc, 1, out)
                self.assertIn(f"⚠️ route {name}:", out)
                self.assertIn("prompt", out)
                self.assertEqual(routes.route(name)["prompt"], "something else")
                fix = re.search(rf"⚠️ route {name}:.*?fix: (.*)", out).group(1)
                self.assertTrue(fix.startswith("remove that entry"), fix)
                routes.remove_route(name)            # the fix line's first step
                command = re.search(r"`hermes dk ([^`]+)`", fix).group(1)
                self.follow(f"`hermes dk {command}`")
                rc, out = self.run_cli(["apply", "--loop", "widgets"])
                self.assertEqual((rc, "already matches" in out), (0, True), out)

    def test_a_refused_registry_profile_is_not_no_profile(self):
        from review_loop import gate_shims
        self.install()
        for bad in ("", "  ", None, 7):
            with self.subTest(profile=bad):
                self.edit_registry("widgets-review", lambda e: e.update(profile=bad))
                detail, fix, status = gate_shims.divergence(config.load_id("widgets"))["widgets-review"]
                self.assertIn("the gateway refuses", detail)
                self.assertIn("apply --loop widgets", fix)
                # Even against a config with no profile (normalize refuses one; a hand-built loop
                # does not): blank in the registry is refused, not "none configured".
                loop = config.load_id("widgets")
                loop["seats"]["reviewer"]["profile"] = ""
                self.assertIn("widgets-review", gate_shims.divergence(loop))
                rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
                self.assertIn("route widgets-review:", out)

    # -- fourth review of #112: every route-check remedy is a command that works ---------------

    def route_checks(self):
        return {c.name: c for c in doctor.check_loop(config.load_id("widgets"), offline=True)
                if c.name.startswith(("route:", "gateway-script:"))}

    def follow_through(self, check):
        """Run the remedy a failed check prints, including a first "remove that entry" step."""
        self.assertNotIn("re-run init", check.fix, "init refuses an existing loop")
        self.assertNotIn("init --", check.fix)
        if check.fix.startswith("remove"):
            routes.remove_route(check.name.split(":", 1)[1])
        self.follow(check.fix)

    def test_a_changed_gateway_origin_is_reconciled_by_the_printed_remedy(self):
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "arbiter")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--host", "https://moved.example"])
        self.assertEqual(rc, 0, out)
        checks = self.route_checks()
        failed = {name: c for name, c in checks.items() if c.failed}
        self.assertIn("route:widgets-review", failed)
        check = failed["route:widgets-review"]
        self.assertIn("origin", check.detail)
        rc, dry = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertNotIn("already matches", dry)
        self.follow_through(check)
        self.assertFalse([c for c in self.route_checks().values() if c.failed],
                         {n: c.detail for n, c in self.route_checks().items() if c.failed})
        for name in ("widgets-review", "widgets-fix", "widgets-breach"):
            self.assertEqual(routes.route(name)["host"], "https://moved.example")
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual((rc, "already matches" in out), (0, True), out)

    def test_no_route_check_remedy_names_init(self):
        mutations = {
            "secret removed": lambda e: e.pop("secret"),
            "events changed": lambda e: e.update(events=["push"]),
            "host elsewhere": lambda e: e.update(host="https://elsewhere.example"),
            "another profile": lambda e: e.update(profile="coder" if e["profile"] != "coder"
                                                  else "critic"),
            "script foreign": lambda e: e.update(script="someone_elses.py"),
            "prompt foreign": lambda e: e.update(prompt="something else"),
        }
        for route in ("widgets-review", "widgets-breach"):
            for label, mutate in mutations.items():
                with self.subTest(route=route, mutation=label):
                    self.setUp()
                    self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                                 "--adjudicator-profile", "arbiter")
                    self.forget_intent()
                    self.edit_registry(route, mutate)
                    failed = [c for c in self.route_checks().values()
                              if c.failed and c.name.endswith(route)]
                    self.assertTrue(failed, "doctor green over a drifted route")
                    self.follow_through(failed[0])
                    left = {n: c.detail for n, c in self.route_checks().items() if c.failed}
                    self.assertFalse(left)

    def test_the_gate_event_rule_has_one_answer(self):
        # contract_drift (apply) and doctor's route checks must agree on each gate's event.
        from review_loop import gate_shims
        for seat in ("reviewer", "fixer"):
            self.assertEqual(gate_shims._GATE_EVENT[seat], doctor.GATE_EVENT[seat])
        self.assertEqual(gate_shims._GATE_EVENT["adjudicator"], "pull_request")


class HookAndCronRemedies(Base):
    """Review of #112 at 4ee0596: `hook:*` and `cron:*` remedies told the operator to re-run init,
    which refuses an existing loop. Every one must now be a command that works, run as printed."""

    REVIEW = "https://gateway.example/p/critic/webhooks/widgets-review"
    FIX = "https://gateway.example/p/coder/webhooks/widgets-fix"

    def world(self, hooks) -> pathlib.Path:
        """A stateful gh stub: list/GET/PATCH (config replaced wholesale, add_events)/POST/DELETE,
        reads masking the secret as GitHub does. Placeholder values only."""
        world = self.tmp / "hooks-world.json"
        world.write_text(json.dumps({"hooks": hooks, "writes": 0}))
        stub = self.tmp / "gh-hooks-world"
        stub.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            path, method = sys.argv[1], os.environ.get("GH_METHOD", "GET")
            body = json.loads(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else {{}}
            with open({str(world)!r}) as f: world = json.load(f)
            def masked(hook):
                config = dict(hook["config"])
                if config.get("secret"):
                    config["secret"] = "********"
                return {{**hook, "config": config}}
            def save():
                world["writes"] += 1
                with open({str(world)!r}, "w") as f: json.dump(world, f)
            if path.split("?")[0].endswith("/hooks") and method == "POST":
                hook = {{"id": max([h["id"] for h in world["hooks"]] + [100]) + 1,
                         "active": body.get("active", True), "events": body.get("events", []),
                         "config": dict(body["config"])}}
                world["hooks"].append(hook); save(); print(json.dumps(masked(hook)))
            elif path.split("?")[0].endswith("/hooks"):
                print(json.dumps([masked(h) for h in world["hooks"]]))
            elif "/hooks/" in path:
                hook_id = path.rsplit("/", 1)[1]
                hook = next((h for h in world["hooks"] if str(h["id"]) == hook_id), None)
                if method == "DELETE":
                    world["hooks"] = [h for h in world["hooks"] if str(h["id"]) != hook_id]
                    save(); print("{{}}")
                elif method == "PATCH":
                    if "config" in body:
                        hook["config"] = dict(body["config"])
                    for event in body.get("add_events", []):
                        if event not in hook["events"]:
                            hook["events"].append(event)
                    save(); print(json.dumps(masked(hook)))
                else:
                    print(json.dumps(masked(hook)))
            else:
                print("{{}}")
        """))
        stub.chmod(0o755)
        os.environ["REVIEW_LOOP_GH_STUB"] = str(stub)
        return world

    @staticmethod
    def hook(hook_id, url, events, content_type="json", active=True):
        return {"id": hook_id, "active": active, "events": list(events),
                "config": {"url": url, "content_type": content_type, "insecure_ssl": "0",
                           "secret": "placeholder-hook-key"}}

    def hook_checks(self):
        return {c.name: c for c in doctor.check_hooks(config.load_id("widgets"), offline=False)}

    def run_printed(self, fix):
        self.assertNotIn("re-run init", fix, "init refuses an existing loop")
        self.assertNotIn("init --", fix)
        command = re.search(r"`hermes dk ([^`]+)`", fix)
        self.assertIsNotNone(command, f"no runnable command in: {fix}")
        argv = shlex.split(command.group(1).replace("<login>", "admin-acct"))
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 0, out)
        return argv, out

    def test_every_hook_remedy_runs_as_printed_and_turns_green(self):
        review, fix = (1, self.REVIEW, ["pull_request"]), (2, self.FIX, ["pull_request_review"])
        cases = {
            "reviewer hook missing": [self.hook(*fix)],
            "hook at another origin": [self.hook(1, "https://old.example/p/critic/webhooks/"
                                                    "widgets-review", ["pull_request"]),
                                       self.hook(*fix)],
            "hook missing its event": [self.hook(1, self.REVIEW, ["push"]), self.hook(*fix)],
            "hook not json": [self.hook(*review, content_type="form"), self.hook(*fix)],
        }
        for label, hooks in cases.items():
            with self.subTest(label):
                self.setUp()
                self.install()
                world = self.world(hooks)
                check = self.hook_checks()["hook:widgets-review"]
                self.assertTrue(check.failed, check.detail)
                command = re.search(r"`hermes dk ([^`]+)`", check.fix)
                self.assertIsNotNone(command, check.fix)
                dry = shlex.split(command.group(1).replace("<login>", "admin-acct")) + ["--dry-run"]
                rc, out = self.run_cli(dry)
                self.assertEqual(json.loads(world.read_text())["writes"], 0, "a dry run wrote")
                self.assertIn("hook", out)
                self.run_printed(check.fix)
                after = self.hook_checks()
                self.assertFalse([c for c in after.values() if c.failed],
                                 {n: c.detail for n, c in after.items()})
                live = json.loads(world.read_text())["hooks"]
                review_hook = next(h for h in live
                                   if routes.route_name_of(h["config"]["url"]) == "widgets-review")
                # A rewritten or created hook carries the route's own secret (full config); an
                # event-only fix leaves the config — secret included — as it was.
                expected = ("placeholder-hook-key" if label == "hook missing its event"
                            else routes.route("widgets-review")["secret"])
                self.assertEqual(review_hook["config"]["secret"], expected)

    def test_cron_remedies_run_as_printed(self):
        self.install()
        check = doctor.check_shim(config.load_id("widgets"))
        self.assertEqual(check.status, doctor.ABSENT)
        self.run_printed(check.fix)
        self.assertEqual(doctor.check_shim(config.load_id("widgets")).status, doctor.VERIFIED)
        doctor.shim_path().write_text("print('stale')\n")
        check = doctor.check_shim(config.load_id("widgets"))
        self.assertEqual(check.status, doctor.MISMATCH)
        self.run_printed(check.fix)
        self.assertEqual(doctor.check_shim(config.load_id("widgets")).status, doctor.VERIFIED)
        job = doctor.check_cron_job(config.load_id("widgets"))
        self.assertEqual(job.status, doctor.ABSENT)
        self.assertNotIn("init", job.fix)
        self.assertIn("hermes cron create", job.fix)
        (doctor.cron_store().parent).mkdir(parents=True, exist_ok=True)
        doctor.cron_store().write_text("not json")
        job = doctor.check_cron_job(config.load_id("widgets"))
        self.assertNotIn("init", job.fix)

_CRON_DRIVER = textwrap.dedent("""
    import argparse, sys
    sys.path.insert(0, sys.argv[1])
    from hermes_cli.subcommands.cron import build_cron_parser
    from hermes_cli.cron import cron_command
    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    sys.exit(cron_command(parser.parse_args(sys.argv[2:])) or 0)
""")


class CronJobRemedies(Base):
    """Review of #112 at dda6d56: `cron:job` printed `hermes cron create` for states where the
    job already exists; create only appends, so the broken job kept answering beside a duplicate.
    Each state's printed remedy, applied, must leave exactly one healthy watchdog job."""

    NAME = "diaktoros watchdog"  # the shared job name (#60): one job sweeps every loop
    BROKEN = {
        "completed": lambda j: j.update(state="completed", enabled=False),
        "paused": lambda j: j.update(state="paused", enabled=False,
                                     paused_at="2026-09-01T00:00:00+00:00"),
        "wrong script": lambda j: j.update(script="something_else.py"),
        "not no-agent": lambda j: j.update(no_agent=False),
        "bad stored schedule": lambda j: j.update(schedule={"kind": "interval", "minutes": 0}),
        "no next_run_at": lambda j: j.update(next_run_at=None),
    }

    @classmethod
    def healthy_job(cls, job_id="aaaaaaaaaaaa"):
        # The shape `hermes cron create 15m --no-agent --script … --deliver local` stores.
        return {"id": job_id, "name": cls.NAME, "script": doctor.SHIM_NAME, "no_agent": True,
                "enabled": True, "state": "scheduled", "deliver": "local",
                "schedule": {"kind": "interval", "minutes": 15, "display": "every 15m"},
                "next_run_at": "2026-09-28T00:00:00+00:00"}

    def store(self, jobs=None):
        path = doctor.cron_store()
        if jobs is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"jobs": jobs}))
        return json.loads(path.read_text())["jobs"]

    def check(self):
        return doctor.check_cron_job(config.load_id("widgets"))

    def named(self):
        return [job for job in self.store() if job.get("name") == self.NAME]

    def commands(self, fix):
        self.assertNotIn("re-run init", fix)
        found = re.findall(r"`hermes (cron [^`]+)`", fix)
        self.assertTrue(found, f"no hermes cron command in: {fix}")
        return [shlex.split(command) for command in found]

    def simulate(self, argv):
        """What Hermes's `cron remove/resume/create` do to the store (cron.jobs), for the suite
        that runs without the Hermes source; the pinned-source test below runs the real ones."""
        jobs = self.store()
        verb = argv[1]
        if verb == "remove":
            jobs = [job for job in jobs if job["id"] != argv[2]]
        elif verb == "resume":
            for job in jobs:
                if job["id"] == argv[2]:
                    job.update(enabled=True, state="scheduled", paused_at=None)
        elif verb == "create":
            jobs.append(self.healthy_job(f"new{len(jobs):09d}"))   # create only appends
        else:
            self.fail(f"unexpected cron verb {verb!r}")
        self.store(jobs)

    def test_no_state_prints_a_create_that_duplicates_an_existing_job(self):
        self.install()
        for label, mutate in self.BROKEN.items():
            with self.subTest(label):
                job = self.healthy_job()
                mutate(job)
                self.store([job])
                check = self.check()
                self.assertTrue(check.failed, check.detail)
                for argv in self.commands(check.fix):
                    self.simulate(argv)
                self.assertEqual(len(self.named()), 1, self.store())
                self.assertEqual(self.check().status, doctor.VERIFIED, self.check().detail)

    def test_two_jobs_with_the_watchdog_name_are_named_and_resolved(self):
        self.install()
        self.store([self.healthy_job("aaaaaaaaaaaa"), self.healthy_job("bbbbbbbbbbbb")])
        check = self.check()
        self.assertTrue(check.failed, "two watchdog jobs both fire the sweep")
        self.assertIn("aaaaaaaaaaaa", check.detail)
        self.assertIn("bbbbbbbbbbbb", check.detail)
        for argv in self.commands(check.fix):
            self.simulate(argv)
        self.assertEqual(len(self.named()), 1)
        self.assertEqual(self.check().status, doctor.VERIFIED)

    def test_a_missing_job_is_created(self):
        self.install()
        self.store([])
        check = self.check()
        self.assertEqual(check.status, doctor.ABSENT)
        commands = self.commands(check.fix)
        self.assertEqual([argv[1] for argv in commands], ["create"])

    def test_an_unwritable_shim_is_refused_not_a_traceback(self):
        self.install()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--watchdog-shim"])
        self.assertEqual(rc, 0, out)
        shim = doctor.shim_path()
        shim.chmod(0o444)
        shim.parent.chmod(0o555)
        try:
            if os.access(shim, os.W_OK):
                self.skipTest("running as a user that ignores file modes")
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--watchdog-shim"])
        finally:
            shim.parent.chmod(0o755)
            shim.chmod(0o755)
        self.assertEqual(rc, 1, out)
        self.assertIn("refused", out)
        self.assertIn(str(shim), out)

    def test_the_printed_remedies_run_through_hermes_own_cron_cli(self):
        """Every state, driven through the pinned Hermes source's `hermes cron` parser and
        handlers in this disposable HOME (never the live install)."""
        if not (SOURCE / "hermes_cli" / "cron.py").exists():
            skip_or_fail(self, f"no Hermes source at {SOURCE}")
        self.install()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--watchdog-shim"])
        self.assertEqual(rc, 0, out)
        driver = self.tmp / "cron_driver.py"
        driver.write_text(_CRON_DRIVER)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
               "HERMES_HOME": str(self.hermes), "TMPDIR": str(self.tmp)}

        def hermes(argv):
            proc = subprocess.run([real_python(), str(driver), str(SOURCE), *argv],
                                  capture_output=True, text=True, timeout=120, env=env,
                                  cwd=str(self.home))
            if proc.returncode != 0 and "No module named" in proc.stderr:
                skip_or_fail(self, f"Hermes cron CLI not importable here: {proc.stderr[-300:]}")
            return proc

        fresh = self.commands(doctor.cron_fix(config.load_id("widgets")))[0]
        for label, mutate in self.BROKEN.items():
            with self.subTest(label):
                if doctor.cron_store().exists():
                    doctor.cron_store().unlink()
                proc = hermes(fresh)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.check().status, doctor.VERIFIED, self.check().detail)
                data = json.loads(doctor.cron_store().read_text())
                mutate(data["jobs"][0])
                doctor.cron_store().write_text(json.dumps(data))
                check = self.check()
                self.assertTrue(check.failed, check.detail)
                for argv in self.commands(check.fix):
                    proc = hermes(argv)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(len(self.named()), 1, self.store())
                self.assertEqual(self.check().status, doctor.VERIFIED, self.check().detail)



class ShimRefusal(unittest.TestCase):
    def test_chmod_names_a_path_that_exists(self):
        """#354: a shim not created yet cannot be chmodded; its directory refused the write."""
        from review_loop import cli
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            scripts = pathlib.Path(tmp) / "scripts"
            scripts.mkdir()
            missing = scripts / "review-loop-watchdog.sh"
            line = cli._shim_refusal(PermissionError(13, "Permission denied", str(missing)))
            self.assertIn(f"cannot write the watchdog shim at {missing}", line)
            self.assertIn(f"chmod u+w -- {scripts}`", line)
            missing.write_text("x")
            line = cli._shim_refusal(PermissionError(13, "Permission denied", str(missing)))
            self.assertIn(f"chmod u+w -- {missing}`", line)

if __name__ == "__main__":
    unittest.main()
