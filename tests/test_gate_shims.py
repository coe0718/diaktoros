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
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
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


# CI's installed-mode job sets this: there the Hermes checkout is the point, so a missing or
# unimportable source is a failure, never a quiet skip.
# TODO: move to #117's tests/hermes_prereqs.py (REQUIRED/needs/skip_or_fail) once it lands.
REQUIRED = os.environ.get("REVIEW_LOOP_REQUIRE_HERMES_SOURCE") == "1"


def real_resolver_available() -> bool:
    present = (SOURCE / "gateway" / "platforms" / "webhook_filters.py").exists()
    if not present and REQUIRED:
        raise AssertionError(f"REVIEW_LOOP_REQUIRE_HERMES_SOURCE=1 but no Hermes source at {SOURCE}"
                             " — set HERMES_AGENT_SOURCE to the hermes-agent checkout")
    return present


def real_resolve(home_env: dict, pairs) -> list:
    """Hermes's own resolver, per (profile, script), under the disposable HOME/HERMES_HOME."""
    proc = subprocess.run([real_python(), "-c", _REAL, str(SOURCE), json.dumps(pairs)],
                          capture_output=True, text=True, timeout=120, env=home_env,
                          cwd=home_env["HOME"])
    if proc.returncode != 0:
        message = f"Hermes resolver not importable from {SOURCE}: {proc.stderr[-300:]}"
        if REQUIRED:
            raise AssertionError(message)
        raise unittest.SkipTest(message)
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
                    "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "review-loops.d"),
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
        for profile in ("vex", "drey", "tuck"):
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
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            args = parser.parse_args(argv)
            rc = args.func(args)
        return rc, out.getvalue()

    def init_argv(self, repo="acme/widgets", *extra):
        return ["init", "--repo", repo, "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "vex", "--fixer-profile", "drey",
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
                            "--adjudicator-profile", "tuck", "--observer-profile", "default")

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
        self.assertTrue((self.hermes / "profiles/vex/scripts/gate_reviewer.py").is_file())
        self.assertTrue((self.hermes / "profiles/drey/scripts/gate_fixer.py").is_file())
        self.assertTrue((self.hermes / "profiles/tuck/scripts/gate_adjudicator.py").is_file())
        self.assertTrue((self.hermes / "scripts/observe.py").is_file())

    def test_hermes_own_resolver_agrees(self):
        if not real_resolver_available():
            self.skipTest(f"no Hermes source at {SOURCE}")
        self.full_install()
        entries = self.loop_routes()
        pairs = [[entry.get("profile", "default"), entry["script"]] for entry in entries.values()]
        real = real_resolve({**os.environ, **self.env}, pairs)
        for (profile, script), (path, error) in zip(pairs, real):
            self.assertIsNone(error, f"{profile}/{script}: {error}")
            self.assertEqual(pathlib.Path(path),
                             (gateway_home(self.hermes, profile) / "scripts" / script).resolve())

    def test_local_resolver_matches_hermes_on_the_edge_cases(self):
        """The copy doctor uses must fail exactly where the gateway fails."""
        if not real_resolver_available():
            self.skipTest(f"no Hermes source at {SOURCE}")
        from review_loop import gate_shims
        scripts = self.hermes / "profiles" / "vex" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "real.py").write_text("print(1)\n")
        (scripts / "adir").mkdir()
        outside = self.tmp / "outside.py"
        outside.write_text("print(1)\n")
        (scripts / "link.py").symlink_to(outside)
        (scripts / "inner-link.py").symlink_to(scripts / "real.py")
        cases = ["real.py", "missing.py", "adir", "link.py", "inner-link.py", "../config.yaml",
                 str(outside), str(scripts / "real.py"), "~/.hermes/profiles/vex/scripts/real.py",
                 "", "  "]
        pairs = [[profile, case] for profile in ("vex", "default") for case in cases]
        real = real_resolve({**os.environ, **self.env}, pairs)
        for (profile, case), want in zip(pairs, real):
            home = gateway_home(self.hermes, profile)
            for ours in (gate_shims.resolve(home, case), gateway_resolve(home, case)):
                got = [str(ours[0]) if ours[0] else None, ours[1]]
                self.assertEqual(got, want, f"{profile}: {case!r}")

    def test_dry_run_names_the_shims_and_writes_none(self):
        rc, out = self.run_cli(self.init_argv("acme/widgets"))  # a real loop for the id
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(self.init_argv("acme/gizmos", "--id", "gizmos",
                                              "--reviewer-profile", "tuck", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"would write: {self.hermes / 'profiles/tuck/scripts/gate_reviewer.py'}", out)
        self.assertFalse((self.hermes / "profiles/tuck/scripts").exists())

    def test_foreign_file_is_refused_before_anything_is_written(self):
        scripts = self.hermes / "profiles" / "vex" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "gate_reviewer.py").write_text("print('mine')\n")
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 2, out)
        self.assertIn("not written by hermes-review-loop", out)
        self.assertEqual((scripts / "gate_reviewer.py").read_text(), "print('mine')\n")
        self.assertFalse((config.config_dir() / "widgets.json").exists())
        self.assertEqual(self.loop_routes(), {})

    def test_init_is_idempotent_and_rewrites_a_stale_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        before = shim.stat()
        self.assertEqual(gate_shims.install(config.load_id("widgets")), [])
        self.assertEqual(shim.stat().st_mtime_ns, before.st_mtime_ns)
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target="/old/plugin/x.py"))
        lines = gate_shims.install(config.load_id("widgets"))
        self.assertEqual(lines, [f"gate shim rewrote: {shim}"])
        self.assertEqual(shim.read_text(), gate_shims.render("gate_reviewer.py"))


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
            ("gate_reviewer.py", self.hermes / "profiles/vex",
             json.dumps({"action": "synchronize", "repository": {"full_name": "acme/widgets"},
                         "pull_request": {"number": 7, "head": {"sha": "a" * 40}}})),
            ("gate_fixer.py", self.hermes / "profiles/drey", "not json"),
            ("gate_adjudicator.py", self.hermes / "profiles/tuck",
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
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER, target=str(probe)))
        via_shim = self.sh([sys.executable, str(shim)], shim_dir, "payload-bytes")
        direct = self.sh([sys.executable, str(probe)], plugin, "payload-bytes")
        self.assertEqual(via_shim.returncode, 3, via_shim.stderr)
        self.assertEqual(json.loads(via_shim.stdout), json.loads(direct.stdout))
        self.assertEqual(json.loads(direct.stdout)["file"], str(probe))

    def test_missing_plugin_script_fails_loudly(self):
        from review_loop import gate_shims
        shim = self.tmp / "shim.py"
        shim.write_text(gate_shims.SHIM.format(marker=gate_shims.MARKER,
                                               target=str(self.tmp / "gone.py")))
        proc = self.sh([sys.executable, str(shim)], self.tmp, "{}")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertIn("gate script missing", proc.stderr)


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
        # (No observer here: `apply` on an observer loop trips an unrelated route-bind check.)
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "tuck")
        checks = self.gateway_checks()
        self.assertEqual(set(checks), {f"gateway-script:widgets-{r}"
                                       for r in ("review", "fix", "breach")})
        self.assertTrue(all(c.status == doctor.VERIFIED for c in checks.values()), checks)

        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        shim.unlink()
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertIn(f"script not found: {shim}", check.detail)
        self.assertIn("hermes review-loop apply --loop widgets", check.fix)

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

        link = self.hermes / "profiles/drey/scripts/gate_fixer.py"
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
        self.edit_registry(lambda d: d["widgets-review"].update(profile="tuck"))
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("registry runs tuck/gate_reviewer.py", check.detail)
        self.assertIn("loop config says vex/gate_reviewer.py", check.detail)
        self.assertIn("hermes review-loop doctor --loop widgets --repair", check.fix)
        # Never silent: install writes the shim the gateway will run *and* says the two disagree.
        from review_loop import gate_shims
        lines = gate_shims.install(config.load_id("widgets"))
        self.assertIn(f"gate shim wrote: {self.hermes / 'profiles/tuck/scripts/gate_reviewer.py'}",
                      lines)
        self.assertTrue(any("registry runs tuck/gate_reviewer.py" in line for line in lines), lines)
        # The named remedy, end to end.
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIn("widgets-review: had changed profile", out)
        self.assertEqual(routes.route("widgets-review")["profile"], "vex")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_hand_edited_config_profile_is_a_mismatch_and_apply_fixes_it(self):
        self.install()
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("registry runs vex/gate_reviewer.py", check.detail)
        self.assertIn("loop config says tuck/gate_reviewer.py", check.detail)
        self.assertIn("hermes review-loop apply --loop widgets", check.fix)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "tuck")
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_blank_config_profile_is_a_mismatch_and_its_remedy_works(self):
        """A seat with no profile: the loader refuses such a file, so this is the in-memory shape a
        pre-validation loop has. The registry still routes the seat, so the gateway still runs it."""
        from review_loop import gate_shims
        self.install()
        loop = config.load_id("widgets")
        loop["seats"]["reviewer"]["profile"] = ""
        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        shim.unlink()
        self.assertEqual(gate_shims.wanted(loop) & {("vex", "gate_reviewer.py")}, set())
        # Not the silent [] from the review: install writes the shim the gateway runs, and says why.
        lines = gate_shims.install(loop)
        self.assertIn(f"gate shim wrote: {shim}", lines)
        self.assertTrue(any("loop config says (no profile)/gate_reviewer.py" in line
                            for line in lines), lines)
        found = {name: (status, detail, fix) for name, status, detail, fix
                 in gate_shims.live_checks(loop)}
        status, detail, fix = found["gateway-script:widgets-review"]
        self.assertEqual(status, "mismatch", detail)
        self.assertIn("registry runs vex/gate_reviewer.py", detail)
        self.assertIn("under `seats.reviewer` in the loop config", fix)
        self.assertIn("hermes review-loop apply --loop widgets", fix)
        # On disk the loader refuses the blank profile by name; the named remedy clears it.
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile=""))
        with self.assertRaisesRegex(config.ConfigError, "seats.reviewer.profile is required"):
            config.load_id("widgets")
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="vex"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        check = self.gateway_checks()["gateway-script:widgets-review"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    def test_route_missing_from_registry_is_named_and_repair_restores_it(self):
        self.install()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.ABSENT, check.detail)
        self.assertIn("loop config says drey/gate_fixer.py", check.detail)
        self.assertIn("registry holds no route", check.detail)
        self.assertIn("hermes review-loop doctor --loop widgets --repair", check.fix)
        self.run_cli(["doctor", "--loop", "widgets", "--repair", "--offline"])
        self.assertIsNotNone(routes.route("widgets-fix"))
        check = self.gateway_checks()["gateway-script:widgets-fix"]
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)

    # -- second review of #106 -------------------------------------------------------------

    def shims(self):
        return [self.hermes / "profiles/vex/scripts/gate_reviewer.py",
                self.hermes / "profiles/drey/scripts/gate_fixer.py"]

    def test_apply_reconciles_a_route_diverged_to_a_missing_profile(self):
        """Tuck's repro: a live pair apply is about to rebind away must not block it."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="ghost"))
        for shim in self.shims():
            shim.unlink()
        rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("route widgets-review: profile ghost → vex", out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "vex")
        for shim in self.shims():
            self.assertTrue(shim.is_file(), shim)
        self.assertTrue(all(c.status == doctor.VERIFIED for c in self.gateway_checks().values()))
        self.assertFalse((self.hermes / "profiles/ghost").exists(), "never create a profile")

    def test_apply_rebinds_away_from_a_foreign_gate_file_but_never_writes_over_one(self):
        self.install()
        foreign = self.hermes / "profiles/tuck/scripts/gate_reviewer.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("print('tuck owns this')\n")
        self.edit_registry(lambda d: d["widgets-review"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "vex")
        self.assertEqual(foreign.read_text(), "print('tuck owns this')\n")
        # A foreign file on a pair apply *would* write still refuses, before anything moves.
        mine = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        mine.write_text("print('vex owns this')\n")
        self.edit_registry(lambda d: d["widgets-review"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 2, out)
        self.assertIn("not written by hermes-review-loop", out)
        self.assertEqual(mine.read_text(), "print('vex owns this')\n")
        self.assertEqual(routes.route("widgets-review")["profile"], "tuck")

    def test_init_dry_run_raises_no_false_alarm_for_routes_it_would_create(self):
        rc, out = self.run_cli(self.init_argv("acme/widgets", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("⚠️", out)
        self.assertNotIn("404", out)
        # On an existing loop a real disagreement is still named.
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(profile="tuck"))
        rc, out = self.run_cli(self.init_argv("acme/widgets", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn("registry runs tuck/gate_reviewer.py, loop config says vex/gate_reviewer.py",
                      out)
        self.assertNotIn("404", out)

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
                   "events": list(events),
                   "config": {"url": other[1], "content_type": "json", "insecure_ssl": "0",
                              "secret": "placeholder-other-hook-key"}}
                  for other in more]
        world.write_text(json.dumps({"hooks": hooks, "patches": 0}))
        stub = self.tmp / "gh-hooks"
        stub.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            path = sys.argv[1]
            world = json.load(open({str(world)!r}))
            method = os.environ.get("GH_METHOD", "GET")
            def masked(hook):
                config = dict(hook["config"])
                if config.get("secret"):
                    config["secret"] = "********"
                return {{**hook, "config": config}}
            if path.endswith("/hooks?per_page=100"):
                print(json.dumps([masked(h) for h in world["hooks"]]))
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
                    json.dump(world, open({str(world)!r}, "w"))
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
        world = self.github_with_hook("https://gateway.example/p/drey/webhooks/widgets-fix")
        remedy = "hermes review-loop apply --loop widgets --recreate-routes"
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
                         ("drey", "gate_fixer.py", "placeholder-recreated-key"))
        self.assertIn("hook 51", out)
        self.assertNotIn("placeholder-recreated-key", out, "never print a secret")
        self.assertEqual(self.hook_config(world),
                         {"url": "https://gateway.example/p/drey/webhooks/widgets-fix",
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
        world = self.github_with_hook("https://gateway.example/p/drey/webhooks/widgets-fix",
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

    def test_a_hook_url_move_keeps_the_hook_secret_under_wholesale_patch(self):
        """apply moving a hook to the seat's new profile sends the whole config, secret included."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        world = self.github_with_hook("https://gateway.example/p/vex/webhooks/widgets-review",
                                      secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertIn("hook 41 → https://gateway.example/p/tuck/webhooks/widgets-review", out)
        self.assertEqual(self.hook_config(world),
                         {"url": "https://gateway.example/p/tuck/webhooks/widgets-review",
                          "content_type": "json", "insecure_ssl": "0",
                          "secret": REVIEW_KEY})
        self.assertNotIn(REVIEW_KEY, out, "never print a secret")

    def test_apply_moves_hooks_as_the_admin_login(self):
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        self.github_with_hook("https://gateway.example/p/vex/webhooks/widgets-review",
                              secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
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
        world = self.github_with_hook("https://gateway.example/p/vex/webhooks/widgets-review",
                                      secret=REVIEW_KEY, hook_id=41, insecure_ssl="1")
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world)["insecure_ssl"], "1")
        # And the re-key of a recreated route.
        route_intent.path(config.load_id("widgets")).unlink()
        self.edit_registry(lambda d: d.pop("widgets-fix"))
        world = self.github_with_hook("https://gateway.example/p/drey/webhooks/widgets-fix",
                                      insecure_ssl="1")
        with patch("secrets.token_hex", return_value="placeholder-recreated-key"):
            rc, out = self.run_cli(["apply", "--loop", "widgets", "--recreate-routes"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world)["insecure_ssl"], "1")

    OLD_FIX = "https://gateway.example/p/vex/webhooks/widgets-fix"      # a profile it left
    NEW_FIX = "https://gateway.example/p/drey/webhooks/widgets-fix"

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
        old = "https://gateway.example/p/vex/webhooks/widgets-review"
        new = "https://gateway.example/p/tuck/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=41, more=[(42, new)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertEqual(routes.route("widgets-review")["profile"], "tuck")
        self.assertEqual(self.hook_config(world, 41)["url"], old, "the redundant hook is not moved")
        self.assertEqual(self.hook_config(world, 42)["url"], new)
        self.assertIn("hook 42 (active) is the one kept", out)
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/41", out)

    def hooks_on(self, world, url):
        return sorted((h["id"], h["active"]) for h in json.loads(world.read_text())["hooks"]
                      if h["config"]["url"] == url)

    def test_a_paused_hook_at_the_target_never_beats_the_live_one(self):
        """Tuck's probe: 41 live on the old URL, 42 paused on the target. The route must end with
        its live hook on its URL, and the paused one named — never the other way round."""
        self.install()
        self.edit_registry(lambda d: d["widgets-review"].update(secret=REVIEW_KEY))
        old = "https://gateway.example/p/vex/webhooks/widgets-review"
        new = "https://gateway.example/p/tuck/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=41,
                                      more=[(42, new, False)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
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
        old = "https://gateway.example/p/vex/webhooks/widgets-review"
        new = "https://gateway.example/p/tuck/webhooks/widgets-review"
        world = self.github_with_hook(old, secret=REVIEW_KEY, hook_id=43, more=[(41, old)])
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.hooks_on(world, new), [(41, True)], "the lowest id moves, alone")
        self.assertEqual(self.hooks_on(world, old), [(43, True)])
        self.assertIn("gh api -X DELETE repos/acme/widgets/hooks/43", out)

    def test_plain_apply_names_a_duplicate_already_on_the_current_url(self):
        self.install()
        url = "https://gateway.example/p/vex/webhooks/widgets-review"
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

    REVIEW_URL = "https://gateway.example/p/vex/webhooks/widgets-review"

    def hook_check(self):
        loop = config.load_id("widgets")
        return {c.name: c for c in doctor.check_hooks(loop, offline=False)}["hook:widgets-review"]

    def test_doctor_does_not_verify_a_hook_whose_url_has_a_trailing_slash(self):
        self.install()
        self.github_with_hook(self.REVIEW_URL + "/", hook_id=41, events=("pull_request",))
        check = self.hook_check()
        self.assertEqual(check.status, doctor.MISMATCH, check.detail)
        self.assertIn("trailing slash", check.detail)
        self.assertIn("hermes review-loop apply --loop widgets", check.fix)

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
        new = "https://gateway.example/p/tuck/webhooks/widgets-review"
        world = self.github_with_hook(self.REVIEW_URL + "/", secret=REVIEW_KEY, hook_id=41)
        self.edit_config(lambda d: d["seats"]["reviewer"].update(profile="tuck"))
        rc, out = self.run_cli(["apply", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.hook_config(world, 41)["url"], new)

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
        self.install("acme/gizmos", "--id", "gizmos", "--fixer-profile", "tuck")
        vex = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
        drey = self.hermes / "profiles/drey/scripts/gate_fixer.py"
        tuck = self.hermes / "profiles/tuck/scripts/gate_fixer.py"
        rc, out = self.run_cli(["uninstall", "--loop", "widgets"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(vex.is_file(), "gizmos still routes its reviewer through vex")
        self.assertFalse(drey.exists())
        self.assertIn(f"gate shim removed: {drey}", out)
        vex.write_text("print('hand edited')\n")      # not ours any more: never removed
        rc, out = self.run_cli(["uninstall", "--loop", "gizmos"])
        self.assertEqual(rc, 0, out)
        self.assertFalse(tuck.exists())
        self.assertEqual(vex.read_text(), "print('hand edited')\n")

    def test_watchdog_heal_restores_a_deleted_shim(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/drey/scripts/gate_fixer.py"
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
        foreign = self.hermes / "profiles/tuck/scripts/observe.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("print('tuck owns this')\n")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--observer-profile", "tuck",
                                "--observer-deliver", "telegram"])
        self.assertEqual(rc, 1, out)
        self.assertIn("gate shim install FAILED", out)
        self.assertIn("hermes review-loop apply --loop widgets", out)
        self.assertEqual(foreign.read_text(), "print('tuck owns this')\n")

    def test_heal_says_what_the_gateway_does_for_each_refusal(self):
        from review_loop import gate_shims
        self.install()
        shim = self.hermes / "profiles/vex/scripts/gate_reviewer.py"
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


if __name__ == "__main__":
    unittest.main()
