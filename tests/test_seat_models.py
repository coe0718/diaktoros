"""Issue #32: each seat's isolated turn runs its own Hermes profile's model, provider and key.

No real Hermes, credentials, ~/.hermes or network: a throwaway HERMES_HOME holds fake profiles,
and a fake ``hermes_cli`` package (the same entry points the resolver imports from the real
Hermes source tree) resolves them. The model upstream is never contacted.
"""
import argparse
import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import cli, config, doctor, gh, seat_model, trusted_turn  # noqa: E402
from review_loop.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40
KEYS = {"rev": "REVIEWER-PROFILE-KEY-1111", "fix": "FIXER-PROFILE-KEY-2222",
        "adj": "ADJUDICATOR-PROFILE-KEY-3333"}

# A miniature of the Hermes entry points the resolver uses. Resolution reads only HERMES_HOME
# and the environment load_hermes_dotenv fills from the profile's .env, like the real one.
FAKE_HERMES = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/env_loader.py": """
        import os
        def load_hermes_dotenv(*, hermes_home=None, **_):
            path = os.path.join(str(hermes_home), ".env")
            if os.path.exists(path):
                for line in open(path):
                    if "=" in line:
                        name, value = line.strip().split("=", 1)
                        os.environ[name] = value
    """,
    "hermes_cli/config.py": """
        import json, os
        def load_config():
            with open(os.path.join(os.environ["HERMES_HOME"], "config.yaml")) as handle:
                return json.load(handle)
    """,
    "hermes_cli/auth.py": """
        class _P:
            def __init__(self, auth_type):
                self.auth_type = auth_type
        PROVIDER_REGISTRY = {"deepseek": _P("api_key"), "someoauth": _P("oauth_device_code")}
    """,
    "hermes_cli/runtime_provider.py": """
        import os
        from hermes_cli.config import load_config
        def _get_model_config():
            return dict(load_config().get("model") or {})
        def resolve_requested_provider(requested=None):
            return str(_get_model_config().get("provider") or "auto").lower()
        def resolve_runtime_provider(**_):
            open(os.path.join(os.environ["HERMES_HOME"], "resolved.marker"), "w").close()
            cfg = _get_model_config()
            provider = resolve_requested_provider()
            if provider == "openrouter":
                key = os.environ.get("OPENROUTER_API_KEY", "")
                if not key:
                    raise RuntimeError("No usable credentials found for provider 'openrouter'.")
                return {"provider": "openrouter", "api_mode": "chat_completions",
                        "base_url": "https://openrouter.test/api/v1", "api_key": key}
            if provider == "deepseek":
                return {"provider": "deepseek", "api_mode": "chat_completions",
                        "base_url": cfg.get("base_url", ""), "api_key": os.environ.get("DEEPSEEK_API_KEY", "")}
            if provider == "geminiish":
                return {"provider": "geminiish", "api_mode": "gemini_native",
                        "base_url": "https://gemini.test", "api_key": "G-KEY-0000"}
            if provider.startswith("custom:"):
                name = provider.split(":", 1)[1]
                for entry in load_config().get("custom_providers") or []:
                    if entry["name"].lower() == name:
                        return {"provider": "custom", "api_mode": "chat_completions",
                                "base_url": entry["base_url"], "api_key": entry["api_key"]}
            raise RuntimeError("Unknown provider " + provider)
    """,
    "hermes_cli/model_catalog.py": """
        def _get_provider_block(provider):
            if provider == "openrouter":
                return {"models": [{"id": "vendor/model-a"}, {"id": "vendor/model-b"}]}
            return None
        def _block_ids(block):
            return [(m["id"], m) for m in (block or {}).get("models", [])]
    """,
}


def write_profile(home: pathlib.Path, name: str, model: dict, env: dict | None = None,
                  extra: dict | None = None) -> pathlib.Path:
    path = home if name == "default" else home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(json.dumps({"model": model, **(extra or {})}))
    if env:
        (path / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        (path / ".env").chmod(0o600)
    return path


def write_yaml_profile(home: pathlib.Path, name: str, model: dict) -> pathlib.Path:
    """A profile whose config.yaml is YAML — comments and block mappings, as Hermes writes it.

    ``write_profile`` writes JSON, which the fake ``hermes_cli`` reads with ``json.load``. That
    covers the describe path only for readers that accept JSON, and a suite that never hands the
    resolver real YAML cannot tell a working reader chain from a broken one (#46).
    """
    path = home if name == "default" else home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    lines = ["# profile config, as Hermes writes it", "model:", f"  default: {model['default']}"]
    if model.get("provider"):
        lines.append(f"  provider: {model['provider']}")
    (path / "config.yaml").write_text("\n".join(lines) + "\n")
    return path


def block_reader(source: pathlib.Path, reader: str) -> None:
    """Shadow ``reader`` in the fake Hermes source, so importing it there raises ImportError.

    The resolver's child puts that directory first on ``sys.path``, so a module here wins over
    anything installed — the same way the interpreter the host picks decides it for real.
    """
    if reader == "yaml":
        (source / "yaml.py").write_text('raise ImportError("no PyYAML in this interpreter")\n')
        return
    (source / "ruamel").mkdir(exist_ok=True)
    (source / "ruamel" / "__init__.py").write_text(
        'raise ImportError("no ruamel.yaml in this interpreter")\n')


def resolver_venv(dest: pathlib.Path) -> str:
    """A venv whose child can import the YAML readers this interpreter has — and nothing else.

    A bare ``bin/python`` symlink is not a venv — no ``pyvenv.cfg``, no ``site-packages`` — so the
    child silently resolves its base interpreter's modules instead, and that is where a YAML reader
    is missing in the first place (#46). Linking the whole site-packages would also make a *real*
    Hermes importable inside the child, which the tests that simulate a broken install rely on not
    happening, so only the readers are linked.
    """
    import sysconfig
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    python = dest / "bin" / "python"
    if not python.exists():
        python.symlink_to(sys.executable)
    (dest / "pyvenv.cfg").write_text(f"home = {pathlib.Path(sys.executable).parent}\n"
                                     "include-system-site-packages = false\n"
                                     f"version = {sys.version.split()[0]}\n")
    packages = dest / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    for entry in pathlib.Path(sysconfig.get_paths()["purelib"]).iterdir():
        if entry.name.split(".")[0] in ("yaml", "ruamel") or entry.name.startswith("_yaml"):
            link = packages / entry.name
            if not link.exists():
                link.symlink_to(entry)
    return str(dest)


def _have(module: str) -> bool:
    """Is ``module`` importable here? ``find_spec`` raises for a dotted name whose parent is
    missing, which is not the same answer as "not installed"."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


HAS_RUAMEL = _have("ruamel.yaml")
HAS_READER = HAS_RUAMEL or _have("yaml")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.home = self.root / "hermes"
        self.home.mkdir()
        source = self.root / "hermes-agent"
        for name, body in FAKE_HERMES.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(textwrap.dedent(body))
        venv = self.root / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(sys.executable)
        self.settings = {"source": str(source), "venv": resolver_venv(venv),
                         "runtime": str(self.root), "rust": str(self.root)}
        write_profile(self.home, "rev", {"default": "vendor/rev-model", "provider": "openrouter"},
                      {"OPENROUTER_API_KEY": KEYS["rev"]})
        write_profile(self.home, "fix", {"default": "fix-model", "provider": "custom:acme",
                                         "base_url": "https://acme.test/v1"},
                      extra={"custom_providers": [{"name": "Acme", "base_url": "https://acme.test/v1",
                                                   "api_key": KEYS["fix"]}]})
        write_profile(self.home, "adj", {"default": "deepseek-chat", "provider": "deepseek",
                                         "base_url": "https://api.deepseek.test/v1"},
                      {"DEEPSEEK_API_KEY": KEYS["adj"]})
        write_profile(self.home, "default", {"default": "claude-x", "provider": "bedrock"})
        self.loop = {"id": "demo", "repo": "acme/widgets", "base": "main", "state_dir": str(self.root / "state"),
                     "unattended_fixer_push": True,
                     "read_token": "reader", "tokens": {},
                     "seats": {"reviewer": {"profile": "rev", "login": "reviewer"},
                               "fixer": {"profile": "fix", "login": "fixer"}},
                     "adjudicator": {"route": "breach", "profile": "adj"}}
        # The parent process holds a provider key of its own (Hermes loads the launch profile's
        # .env into os.environ): it must never satisfy a seat's resolution.
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")}
        env.update(HERMES_HOME=str(self.home), OPENROUTER_API_KEY="PARENT-PROCESS-LEAK-9999")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def key_file(self, name: str, value: str) -> str:
        path = self.root / name
        path.write_text(value + "\n")
        path.chmod(0o600)
        return str(path)


class ProfileResolution(Base):
    def test_each_seat_gets_its_own_profile_model_provider_and_key(self):
        got = {seat: seat_model.resolve_seat(self.loop, seat, self.settings)
               for seat in ("reviewer", "fixer", "adjudicator")}
        self.assertEqual(
            {seat: (i.origin, i.profile, i.provider, i.model, i.upstream, i.key) for seat, i in got.items()},
            {"reviewer": ("profile", "rev", "openrouter", "vendor/rev-model",
                          "https://openrouter.test/api/v1/chat/completions", KEYS["rev"]),
             "fixer": ("profile", "fix", "custom:acme", "fix-model",
                       "https://acme.test/v1/chat/completions", KEYS["fix"]),
             "adjudicator": ("profile", "adj", "deepseek", "deepseek-chat",
                             "https://api.deepseek.test/v1/chat/completions", KEYS["adj"])})
        self.assertEqual(len({i.identity() for i in got.values()}), 3)
        for inference in got.values():
            self.assertNotIn(inference.key, repr(inference))
            self.assertNotIn(inference.key, inference.describe())

    def test_parent_environment_key_never_satisfies_a_seat(self):
        (self.home / "profiles" / "rev" / ".env").unlink()
        with self.assertRaises(seat_model.SeatModelError) as caught:
            seat_model.resolve_seat(self.loop, "reviewer", self.settings)
        self.assertIn("No usable credentials", str(caught.exception))
        self.assertNotIn("PARENT-PROCESS-LEAK", str(caught.exception))

    def test_unsupported_providers_are_refused_before_credentials_are_touched(self):
        for provider in ("bedrock", "copilot", "someoauth", "auto"):
            with self.subTest(provider=provider):
                path = write_profile(self.home, "rev", {"default": "m", "provider": provider})
                with self.assertRaisesRegex(seat_model.SeatModelError, "rev"):
                    seat_model.resolve_seat(self.loop, "reviewer", self.settings)
                self.assertFalse((path / "resolved.marker").exists())

    def test_unknown_api_mode_is_refused(self):
        write_profile(self.home, "rev", {"default": "m", "provider": "geminiish"})
        with self.assertRaisesRegex(seat_model.SeatModelError, "gemini_native.*cannot speak"):
            seat_model.resolve_seat(self.loop, "reviewer", self.settings)

    def test_plain_http_provider_is_refused(self):
        write_profile(self.home, "adj", {"default": "d", "provider": "deepseek",
                                         "base_url": "http://api.deepseek.test/v1"},
                      {"DEEPSEEK_API_KEY": KEYS["adj"]})
        with self.assertRaisesRegex(seat_model.SeatModelError, "HTTPS"):
            seat_model.resolve_seat(self.loop, "adjudicator", self.settings)

    def test_missing_profile_holds_with_a_reason(self):
        loop = {**self.loop, "seats": {**self.loop["seats"], "fixer": {"profile": "ghost"}}}
        with self.assertRaisesRegex(seat_model.SeatModelError, "profile ghost does not exist"):
            seat_model.resolve_seat(loop, "fixer", self.settings)
        with self.assertRaisesRegex(seat_model.SeatModelError, "no Hermes profile"):
            seat_model.resolve_seat({**loop, "seats": {"fixer": {}}}, "fixer", self.settings)

    def test_hermes_unavailable_fails_closed(self):
        settings = {**self.settings, "source": str(self.root / "nowhere")}
        with self.assertRaisesRegex(seat_model.SeatModelError, "not importable"):
            seat_model.resolve_seat(self.loop, "reviewer", settings)


class Precedence(Base):
    def test_per_seat_override_beats_the_profile(self):
        settings = {**self.settings, "seats": {"fixer": {
            "model": "override-model", "upstream": "https://override.test/v1/chat/completions",
            "key_file": self.key_file("fixer.key", "OVERRIDE-KEY-4444")}}}
        fixer = seat_model.resolve_seat(self.loop, "fixer", settings)
        self.assertEqual((fixer.origin, fixer.model, fixer.key), ("override", "override-model",
                                                                  "OVERRIDE-KEY-4444"))
        self.assertEqual(seat_model.resolve_seat(self.loop, "reviewer", settings).origin, "profile")

    def test_legacy_trio_only_when_the_profile_cannot_resolve(self):
        settings = {**self.settings, "model": "legacy-model",
                    "upstream": "https://legacy.test/v1/chat/completions",
                    "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")}
        self.assertEqual(seat_model.resolve_seat(self.loop, "reviewer", settings).origin, "profile")
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        adj = seat_model.resolve_seat(loop, "adjudicator", settings)
        self.assertEqual((adj.origin, adj.model), ("legacy", "legacy-model"))
        self.assertIn("bedrock", adj.warning)

    def test_runtime_file_shapes(self):
        base = {k: "/x" for k in seat_model.HOST_KEYS}
        seat_model.parse_runtime(base)
        seat_model.parse_runtime({**base, "model": "m", "upstream": "u", "key_file": "k"})
        seat_model.parse_runtime({**base, "seats": {"reviewer": {"model": "m", "upstream": "u",
                                                                 "key_file": "k"}}})
        for bad in ({**base, "model": "m"}, {k: "/x" for k in ("source", "venv", "runtime")},
                    {**base, "extra": 1}, {**base, "seats": {"observer": {}}},
                    {**base, "seats": {"fixer": {"model": "m"}}}, [], {**base, "source": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                seat_model.parse_runtime(bad)


class Worker(Base):
    """The production worker hands run_turn the row's own seat resolution, or holds the run."""

    def run_seat(self, seat: str, settings: dict | None = None, loop: dict | None = None):
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps(settings or self.settings))
        runtime.chmod(0o600)
        sup = Supervisor(self.root / "ledger.sqlite", production_config=runtime, hermes_home=self.home)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue(f"d-{seat}", "acme/widgets", 7, HEAD, seat)
        with sqlite3.connect(sup.db) as con:
            # A fixer row is launched only when admitted with pushes on (the gate holds it otherwise).
            con.execute("UPDATE runs SET state='launching', owner='w', generation='g', "
                        "push_admitted=1 WHERE delivery=?", (f"d-{seat}",))
            run_id = con.execute("SELECT id FROM runs WHERE delivery=?", (f"d-{seat}",)).fetchone()[0]
        seen = {}

        def run_turn(_loop, scope, **kw):
            seen.update(kw, role=scope.role)
            return 0
        pr = {"number": 7, "head": {"sha": HEAD, "ref": "fix-7"}}
        from review_loop import run_supervisor
        with mock.patch.object(config, "by_repo", return_value=loop or self.loop), \
             mock.patch.object(gh, "api", return_value=pr) as api, \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"), \
             mock.patch("review_loop.gate.latest_effective_review_at_head", return_value={}), \
             mock.patch.object(run_supervisor, "adjudication_state",
                               return_value=("ok", {"reviews": [], "marker": {"rounds": 3}})), \
             mock.patch("review_loop.state.state_for") as state_for, \
             mock.patch.object(run_supervisor, "isolated_prompt", return_value="PROMPT"), \
             mock.patch.object(run_supervisor, "pr_change",
                               return_value=run_supervisor.PRChange("CHANGE", "DIFF")), \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            state_for.return_value.breach_start.return_value = {"ok": True}
            sup._run_production(run_id, "w")
            called_github = api.called
        with sqlite3.connect(sup.db) as con:
            row = con.execute("SELECT state, error FROM runs WHERE id=?", (run_id,)).fetchone()
        return seen, row, called_github

    def test_each_seat_turn_runs_with_its_own_model_and_key(self):
        expected = {"reviewer": ("vendor/rev-model", KEYS["rev"], "openrouter.test"),
                    "fixer": ("fix-model", KEYS["fix"], "acme.test"),
                    "adjudicator": ("deepseek-chat", KEYS["adj"], "api.deepseek.test")}
        for seat, (model, key, host) in expected.items():
            with self.subTest(seat=seat):
                seen, row, _ = self.run_seat(seat)
                self.assertEqual(row, ("succeeded", None))
                self.assertEqual((seen["role"], seen["model"], seen["key"]), (seat, model, key))
                self.assertIn(host, seen["upstream"])
                # Reviewer and fixer get the staged diff (#50); a ruling has none.
                self.assertEqual(seen["review_diff"], None if seat == "adjudicator" else "DIFF")
                others = {k for s, (_, k, _) in expected.items() if s != seat}
                self.assertFalse(others & {seen["key"]})

    def test_unresolvable_seat_is_held_before_any_github_read(self):
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        seen, row, called_github = self.run_seat("adjudicator", loop=loop)
        self.assertEqual(seen, {})
        self.assertFalse(called_github)
        self.assertEqual(row[0], "failed")
        self.assertRegex(row[1], r"^seat model unresolved: profile default \(bedrock\).*cloud signing")

    def test_legacy_seven_key_runtime_still_runs(self):
        settings = {**self.settings, "model": "legacy-model",
                    "upstream": "https://legacy.test/v1/chat/completions",
                    "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")}
        loop = {**self.loop, "seats": {**self.loop["seats"], "reviewer": {"profile": "ghost"}}}
        seen, row, _ = self.run_seat("reviewer", settings, loop)
        self.assertEqual(row, ("succeeded", None))
        self.assertEqual((seen["model"], seen["key"]), ("legacy-model", "LEGACY-KEY-5555"))


class DoctorAndModels(Base):
    def write_runtime(self, settings: dict) -> None:
        path = self.home / "review-loop-runtime.json"
        path.write_text(json.dumps(settings))
        path.chmod(0o600)

    def test_doctor_shows_each_seat_without_touching_credentials(self):
        self.write_runtime(self.settings)
        checks = {c.name: c for c in doctor.check_seat_models(self.loop)}
        self.assertEqual({n: c.status for n, c in checks.items()},
                         {"model:reviewer": doctor.VERIFIED, "model:fixer": doctor.VERIFIED,
                          "model:adjudicator": doctor.VERIFIED})
        self.assertIn("rev: openrouter / vendor/rev-model", checks["model:reviewer"].detail)
        self.assertIn("custom:acme / fix-model via acme.test", checks["model:fixer"].detail)
        for name in ("rev", "fix", "adj"):
            self.assertFalse((self.home / "profiles" / name / "resolved.marker").exists())
        text = json.dumps([vars(c) for c in checks.values()])
        for key in KEYS.values():
            self.assertNotIn(key, text)

    def test_doctor_fails_an_unresolvable_seat_and_warns_on_legacy(self):
        loop = {**self.loop, "adjudicator": {"route": "breach", "profile": "default"}}
        self.write_runtime(self.settings)
        checks = {c.name: c for c in doctor.check_seat_models(loop)}
        self.assertEqual(checks["model:adjudicator"].status, doctor.ABSENT)
        self.assertIn("will be held", checks["model:adjudicator"].detail)
        self.write_runtime({**self.settings, "model": "legacy-model",
                            "upstream": "https://legacy.test/v1/chat/completions",
                            "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")})
        checks = {c.name: c for c in doctor.check_seat_models(loop)}
        self.assertEqual(checks["model:adjudicator"].status, doctor.UNKNOWN)
        self.assertIn("LEGACY", checks["model:adjudicator"].detail)
        self.assertEqual(checks["runtime:legacy-model"].status, doctor.UNKNOWN)

    def models(self, **kw):
        self.write_runtime(self.settings)
        out = io.StringIO()
        args = argparse.Namespace(profile=kw.get("profile"), seat=kw.get("seat"), loop=None)
        with redirect_stdout(out), mock.patch.object(config, "all_loops", return_value=[self.loop]):
            rc = cli.cmd_models(args)
        return rc, out.getvalue()

    def test_models_lists_the_catalog_for_a_profile_or_seat(self):
        rc, text = self.models(profile="rev")
        self.assertEqual(rc, 0, text)
        self.assertIn("provider openrouter", text)
        self.assertIn("vendor/model-a", text)
        self.assertIn("not in this list", text)            # current model vendor/rev-model
        rc, text = self.models(seat="reviewer")
        self.assertEqual(rc, 0, text)
        self.assertIn("profile rev (seat reviewer)", text)
        self.assertNotIn(KEYS["rev"], text)

    def test_models_is_loud_about_unknown_providers_and_profiles(self):
        rc, text = self.models(profile="adj")
        self.assertEqual(rc, 1)
        self.assertIn("unknown to the Hermes model catalog", text)
        rc, text = self.models(profile="ghost")
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", text)


class ReaderChain(Base):
    """#46: which YAML reader the resolver's interpreter has decides whether a seat can run.

    A packaged install runs Hermes on its own bundled python, which ships neither PyYAML nor
    ruamel.yaml. The child had one reader and a JSON fallback, so on that interpreter every seat
    failed with "profile config.yaml unreadable" — for a config that was perfectly fine.
    """

    def describe(self, profile: str = "rev") -> dict:
        return seat_model.run_resolver(profile, "describe", self.settings)

    @unittest.skipUnless(HAS_READER, "needs a YAML reader in this interpreter")
    def test_a_real_yaml_config_is_read(self):
        write_yaml_profile(self.home, "rev",
                           {"default": "vendor/rev-model", "provider": "openrouter"})
        answer = self.describe()
        self.assertEqual((answer.get("model"), answer.get("requested")),
                         ("vendor/rev-model", "openrouter"))

    @unittest.skipUnless(HAS_RUAMEL, "needs ruamel.yaml, the reader Hermes itself ships")
    def test_ruamel_alone_is_enough(self):
        block_reader(self.root / "hermes-agent", "yaml")
        write_yaml_profile(self.home, "rev",
                           {"default": "vendor/rev-model", "provider": "openrouter"})
        answer = self.describe()
        self.assertNotIn("error", answer)
        self.assertEqual(answer.get("model"), "vendor/rev-model")

    def test_no_reader_at_all_names_the_interpreter_not_the_config(self):
        for reader in ("yaml", "ruamel"):
            block_reader(self.root / "hermes-agent", reader)
        write_yaml_profile(self.home, "rev",
                           {"default": "vendor/rev-model", "provider": "openrouter"})
        answer = self.describe()
        self.assertEqual(answer.get("kind"), "interpreter")
        self.assertIn("no YAML library", str(answer.get("error")))
        self.assertIn("review-loop-runtime.json", str(answer.get("error")))
        self.assertNotIn("config.yaml unreadable", str(answer.get("error")))

    def test_a_json_config_still_resolves_with_no_reader(self):
        for reader in ("yaml", "ruamel"):
            block_reader(self.root / "hermes-agent", reader)
        answer = self.describe()                    # the fixture's profiles are JSON documents
        self.assertEqual(answer.get("model"), "vendor/rev-model")

    def test_doctor_names_the_runtime_file_when_the_interpreter_cannot_read_yaml(self):
        for reader in ("yaml", "ruamel"):
            block_reader(self.root / "hermes-agent", reader)
        # the seat's own config, in the form a reader-less interpreter cannot parse at all
        write_yaml_profile(self.home, "rev",
                           {"default": "vendor/rev-model", "provider": "openrouter"})
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps(self.settings))
        runtime.chmod(0o600)
        checks = {c.name: c for c in doctor.check_seat_models(self.loop)}
        self.assertEqual(checks["model:reviewer"].status, doctor.ABSENT)
        self.assertIn("will be held", checks["model:reviewer"].detail)
        self.assertIn("review-loop-runtime.json", checks["model:reviewer"].fix)
        self.assertNotIn("set a supported provider", checks["model:reviewer"].fix)


class ProviderExtras(Base):
    """#118: a seat whose provider needs an optional Hermes extra (``anthropic`` for a Claude
    subscription or any Messages-wire provider) fails at model setup when the runtime's venv lacks
    it. doctor asks that venv's own python — ``find_spec``, read-only — and never a credential.

    The two venvs are throwaway directories around this interpreter (no packages installed, no
    network): one gets a stub ``anthropic`` package in its site-packages, the other does not.
    """

    def setUp(self):
        super().setUp()
        self.bare = self.settings["venv"]                       # Base's venv: no anthropic
        full = self.root / "venv-with-anthropic"
        resolver_venv(full)
        site = next((full / "lib").glob("python*/site-packages"))
        (site / "anthropic").mkdir()
        (site / "anthropic" / "__init__.py").write_text("")
        self.full = str(full)
        write_profile(self.home, "rev", {"default": "claude-sonnet", "provider": "anthropic"})

    def extras(self, venv: str | None = None, loop: dict | None = None, settings: dict | None = None):
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps(settings or {**self.settings, "venv": venv or self.bare}))
        runtime.chmod(0o600)
        return {c.name: c for c in doctor.check_seat_extras(loop or self.loop)}

    def test_the_table_names_the_anthropic_extra_and_its_import(self):
        self.assertEqual(seat_model.HERMES_EXTRAS["anthropic"]["module"], "anthropic")
        need = lambda *a, **k: seat_model.extras_for(*a, **k)[0]
        self.assertEqual(need("anthropic", "anthropic_messages", ""), ["anthropic"])
        self.assertEqual(need("claude-code", "", ""), ["anthropic"])
        self.assertEqual(need("minimax-oauth", "anthropic_messages", ""), ["anthropic"])
        self.assertEqual(need("custom:acme", "anthropic_messages", ""), ["anthropic"])
        self.assertEqual(need("custom:acme", "", "https://gw.test/anthropic"), ["anthropic"])
        self.assertEqual(need("custom:acme", "", "https://gw.test/anthropic/v1"), ["anthropic"])
        self.assertEqual(need("custom:acme", "", "https://api.kimi.com/coding/v1"), ["anthropic"])
        self.assertEqual(seat_model.extras_for("openrouter", "chat_completions", ""), ([], []))

    def test_a_model_that_picks_the_messages_wire_needs_it(self):
        # OpenCode's per-model wire table (hermes_cli/models.py _OPENCODE_API_MODE_PREFIXES)
        need = lambda p, m: seat_model.extras_for(p, "chat_completions", "", model=m)
        self.assertEqual(need("opencode-zen", "claude-sonnet-4-6"), (["anthropic"], []))
        self.assertEqual(need("opencode", "opencode-zen/qwen3-coder"), (["anthropic"], []))
        self.assertEqual(need("opencode-go", "minimax-m2.7"), (["anthropic"], []))
        self.assertEqual(need("opencode-zen", "gpt-5"), ([], []))
        self.assertEqual(need("opencode-go", "deepseek-v4-flash"), ([], []))

    def test_a_provider_that_can_switch_wire_is_possible(self):
        # hermes_cli/providers.py nous_api_mode: Messages only with nous.anthropic_wire native;
        # agent/nous_wire.py promotes only on auto. Unset/chat never reaches it: no extra at all.
        nous = lambda **facts: seat_model.extras_for("nous", "chat_completions", "",
                                                     model="anthropic/claude-x", facts=facts)
        self.assertEqual(nous(), ([], []))
        self.assertEqual(nous(nous_anthropic_wire="chat"), ([], []))
        self.assertEqual(nous(nous_anthropic_wire="auto"), ([], ["anthropic"]))
        self.assertEqual(nous(nous_anthropic_wire="native"), (["anthropic"], []))
        self.assertEqual(seat_model.extras_for("nous", "chat_completions", "", model="openai/gpt-5",
                                               facts={"nous_anthropic_wire": "native"}), ([], []))
        self.assertEqual(seat_model.extras_for("kimi-coding", "", "", model="kimi-k2.6"),
                         ([], ["anthropic"]))
        self.assertEqual(seat_model.extras_for("kimi", "", "https://api.moonshot.ai/v1",
                                               model="kimi-k2.6"), ([], []))

    def test_an_explicit_api_mode_beats_a_provider_or_host_match(self):
        # runtime_provider: a configured model.api_mode beats URL detection and the overlay's
        # transport (_configured_or_fallback_api_mode, _custom_runtime) ...
        need = lambda *a, **k: seat_model.extras_for(*a, **k)
        self.assertEqual(need("custom:acme", "chat_completions", "https://api.anthropic.com",
                              configured="chat_completions"), ([], []))
        self.assertEqual(need("custom:acme", "chat_completions", "https://gw.test/anthropic",
                              configured="openai"), ([], []))            # Hermes's alias for chat
        self.assertEqual(need("minimax", "chat_completions", "", configured="chat_completions"),
                         ([], []))
        self.assertEqual(need("kimi-coding", "chat_completions", "", model="kimi-k2.6",
                              configured="chat_completions"), ([], []))
        self.assertEqual(need("custom:acme", "chat_completions", "https://api.anthropic.com"),
                         (["anthropic"], []))                            # nothing configured
        # ... except where Hermes pins the wire itself: anthropic (runtime_provider.py: provider
        # anthropic → anthropic_messages), minimax-oauth (_minimax_oauth_runtime), nous
        # (nous_api_mode) and the built-in OpenCode families (opencode_by_model).
        self.assertEqual(need("anthropic", "anthropic_messages", "", configured="chat_completions"),
                         (["anthropic"], []))
        self.assertEqual(need("minimax-oauth", "anthropic_messages", "",
                              configured="chat_completions"), (["anthropic"], []))
        self.assertEqual(need("opencode-zen", "chat_completions", "", model="claude-sonnet-4-6",
                              configured="chat_completions"), (["anthropic"], []))
        self.assertEqual(need("opencode-go-bridge", "chat_completions", "", model="minimax-m2",
                              configured="chat_completions"), ([], []))  # custom: its api_mode wins

    def test_a_possible_switch_without_the_package_is_a_warning_with_the_fix(self):
        write_profile(self.home, "rev", {"default": "anthropic/claude-x", "provider": "nous"},
                      extra={"nous": {"anthropic_wire": "auto"}})
        check = self.extras(self.bare)["extras:reviewer"]
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("may need the anthropic extra: nous can use Anthropic's native wire for "
                      "anthropic/claude-x", check.detail)
        self.assertIn("hermes pm install --extra anthropic", check.detail)
        self.assertEqual(self.extras(self.full)["extras:reviewer"].status, doctor.VERIFIED)
        for config in ({}, {"nous": {"anthropic_wire": "chat"}}):       # never the Messages wire
            write_profile(self.home, "rev", {"default": "anthropic/claude-x", "provider": "nous"},
                          extra=config)
            check = self.extras(self.bare)["extras:reviewer"]
            self.assertEqual(check.status, doctor.VERIFIED, config)
            self.assertIn("needs no optional", check.detail)

    def test_a_malformed_url_is_unknown_and_never_escapes_the_preflight(self):
        write_profile(self.home, "rev", {"default": "m", "provider": "custom:acme",
                                         "base_url": "https://[::1"})
        check = self.extras(self.full)["extras:reviewer"]
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("ValueError", check.detail)
        wire = ("ok", "", "", {"provider": "custom:acme", "api_mode": "chat_completions",
                               "base_url": "https://[::1", "model": "m", "facts": {}})
        with mock.patch.object(seat_model, "describe_seat_wire", return_value=wire):
            check = self.extras(self.full)["extras:reviewer"]
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("ValueError", check.detail)
        models = {c.name: c for c in doctor.check_seat_models(self.loop)}
        self.assertEqual(models["model:reviewer"].status, doctor.UNKNOWN)
        self.assertIn("ValueError", models["model:reviewer"].detail)
        names = [c.name for c in doctor.check_loop(self.loop, offline=True)]
        self.assertIn("extras:reviewer", names)

    def test_nous_native_wire_in_the_profile_is_required(self):
        write_profile(self.home, "rev", {"default": "anthropic/claude-x", "provider": "nous"},
                      extra={"nous": {"anthropic_wire": "native"}})
        self.assertEqual(self.extras(self.bare)["extras:reviewer"].status, doctor.ABSENT)

    def test_an_unresolved_model_skips_the_extras_line(self):
        write_profile(self.home, "fix", {"default": "fix-model"})              # no provider
        self.write_runtime_for(self.full)
        models = {c.name: c for c in doctor.check_seat_models(self.loop)}
        self.assertEqual(models["model:fixer"].status, doctor.ABSENT)
        check = {c.name: c for c in doctor.check_seat_extras(self.loop)}["extras:fixer"]
        self.assertEqual(check.status, doctor.SKIPPED)
        self.assertFalse(check.failed)
        self.assertEqual(check.detail, "skipped: model unresolved (see model:fixer)")
        out = io.StringIO()
        with redirect_stdout(out):
            doctor.report(self.loop, [check])
        self.assertIn("skipped: model unresolved (see model:fixer)", out.getvalue())
        self.assertIn("0 unknown", out.getvalue())
        self.assertIn("1 skipped", out.getvalue())

    def write_runtime_for(self, venv: str) -> None:
        runtime = self.home / "review-loop-runtime.json"
        runtime.write_text(json.dumps({**self.settings, "venv": venv}))
        runtime.chmod(0o600)

    def test_a_venv_with_the_package_passes(self):
        checks = self.extras(self.full)
        self.assertEqual(checks["extras:reviewer"].status, doctor.VERIFIED)
        self.assertIn("anthropic", checks["extras:reviewer"].detail)
        self.assertIn(self.full, checks["extras:reviewer"].detail)
        self.assertEqual(checks["extras:fixer"].status, doctor.VERIFIED)       # custom:acme
        self.assertIn("no optional", checks["extras:fixer"].detail)

    def test_a_venv_without_the_package_fails_with_the_install_command(self):
        checks = self.extras(self.bare)
        check = checks["extras:reviewer"]
        self.assertEqual(check.status, doctor.ABSENT)
        self.assertTrue(check.failed)
        self.assertIn("anthropic", check.detail)
        self.assertIn(self.bare, check.detail)
        self.assertIn("hermes pm install --extra anthropic", check.fix)
        self.assertIn("review-loop-runtime.json", check.fix)
        self.assertEqual(checks["extras:fixer"].status, doctor.VERIFIED)
        self.assertEqual(checks["extras:adjudicator"].status, doctor.VERIFIED)

    def test_a_messages_wire_provider_needs_it_too(self):
        write_profile(self.home, "fix", {"default": "m2", "provider": "minimax-oauth"})
        checks = self.extras(self.bare)
        self.assertEqual(checks["extras:fixer"].status, doctor.ABSENT)

    def test_it_never_resolves_a_credential(self):
        write_profile(self.home, "rev", {"default": "claude-sonnet", "provider": "anthropic"},
                      {"ANTHROPIC_TOKEN": "SUBSCRIPTION-TOKEN-7777"})
        checks = self.extras(self.bare)
        for name in ("rev", "fix", "adj"):
            self.assertFalse((self.home / "profiles" / name / "resolved.marker").exists())
        text = json.dumps([vars(c) for c in checks.values()])
        for secret in (*KEYS.values(), "SUBSCRIPTION-TOKEN-7777"):
            self.assertNotIn(secret, text)

    def test_an_unreadable_provider_is_skipped_or_unknown_never_verified(self):
        (self.home / "profiles" / "rev" / "config.yaml").write_text("{ not: [valid")
        write_profile(self.home, "fix", {"default": "fix-model"})              # no provider
        loop = {**self.loop, "seats": {**self.loop["seats"], "adjudicator": {"profile": "ghost"}},
                "adjudicator": {"route": "breach", "profile": "ghost"}}
        checks = self.extras(self.full, loop)
        for seat in ("reviewer", "fixer", "adjudicator"):
            self.assertEqual(checks[f"extras:{seat}"].status, doctor.SKIPPED, seat)
        undecided = ("warn", "profile rev: Hermes gave no answer", "run selftest", None)
        with mock.patch.object(seat_model, "describe_seat_wire", return_value=undecided):
            check = self.extras(self.full)["extras:reviewer"]
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("could not be read", check.detail)

    def test_the_probe_asks_the_venvs_own_python(self):
        full, bare = (str(pathlib.Path(v) / "bin" / "python") for v in (self.full, self.bare))
        self.assertIs(doctor.venv_has_module(full, "anthropic"), True)
        self.assertIs(doctor.venv_has_module(bare, "anthropic"), False)
        self.assertIs(doctor.venv_has_module(bare, "anthropic.types"), False)   # parent missing

    def test_a_python_that_cannot_answer_is_undecided(self):
        odd = self.root / "odd" / "python"
        odd.parent.mkdir()
        odd.write_text("#!/bin/sh\nexit 3\n")
        odd.chmod(0o755)
        self.assertIsNone(doctor.venv_has_module(str(odd), "anthropic"))
        odd.write_text("#!/bin/sh\nexec sleep 5\n")
        with mock.patch.object(doctor, "EXTRA_PROBE_TIMEOUT", 0.3):
            self.assertIsNone(doctor.venv_has_module(str(odd), "anthropic"))
        self.assertIsNone(doctor.venv_has_module(str(self.root / "missing" / "python"), "anthropic"))

    def test_an_undecided_probe_is_unknown_not_verified(self):
        with mock.patch.object(doctor, "venv_has_module", return_value=None):
            check = self.extras(self.full)["extras:reviewer"]
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("could not answer", check.detail)

    def test_no_runtime_file_is_unknown(self):
        checks = {c.name: c for c in doctor.check_seat_extras(self.loop)}
        self.assertEqual(checks["extras:reviewer"].status, doctor.UNKNOWN)
        self.assertIn("review-loop-runtime.json", checks["extras:reviewer"].detail)

    def test_a_runtime_override_needs_no_extra(self):
        settings = {**self.settings, "seats": {"reviewer": {
            "model": "m", "upstream": "https://o.test/v1/chat/completions",
            "key_file": self.key_file("o.key", "OVERRIDE-KEY-8888")}}}
        self.assertEqual(self.extras(settings=settings)["extras:reviewer"].status, doctor.VERIFIED)

    def test_the_legacy_fallback_is_chat_completions(self):
        loop = {**self.loop, "seats": {**self.loop["seats"], "reviewer": {"profile": "ghost"}}}
        settings = {**self.settings, "model": "legacy-model",
                    "upstream": "https://legacy.test/v1/chat/completions",
                    "key_file": self.key_file("legacy.key", "LEGACY-KEY-5555")}
        self.assertEqual(self.extras(loop=loop, settings=settings)["extras:reviewer"].status,
                         doctor.VERIFIED)

    def test_the_preflight_runs_the_check(self):
        self.extras(self.bare)
        with mock.patch.object(doctor, "check_seat_extras", wraps=doctor.check_seat_extras) as spy:
            checks = doctor.check_loop(self.loop, offline=True)
        spy.assert_called_once()
        self.assertIn("extras:reviewer", [c.name for c in checks])


if __name__ == "__main__":
    unittest.main()
