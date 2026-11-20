"""#2374: the seat resolver and the DirectSDK child read a profile's secrets through Hermes's
``agent.secret_scope``, never by loading its ``.env`` into ``os.environ``.

A fake Hermes in a throwaway source tree, shaped like the real one:
- ``load_hermes_dotenv`` loads the ``.env`` and the external secret sources into ``os.environ``, so
  a run that still calls it shows up as a key in the environment;
- the external sources (``source.env`` here, standing in for a command helper or 1Password) reach
  ``build_profile_secret_scope`` only through the per-home snapshot that
  ``hydrate_profile_secret_sources`` fills, so a run that skips it loses them.

No real Hermes, credentials or network.
"""
import _home_guard
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from diaktoros import config, directsdk_backend as backend, seat_model

KEY = "profile-key-placeholder-2374"
OTHER = "unrelated-secret-placeholder-2374"   # a profile secret no step of the turn asks for

FAKE_SCOPE = """
import os
_SCOPE = None
def build_profile_secret_scope(hermes_home):
    from hermes_cli.env_loader import _read, get_secret_source_values
    return {**_read(os.path.join(str(hermes_home), ".env")), **get_secret_source_values(hermes_home)}
def set_secret_scope(secrets, *, profile_home=None):
    global _SCOPE
    _SCOPE = dict(secrets)
def get_secret(name, default=None):
    if _SCOPE is not None and name in _SCOPE:
        return _SCOPE[name]
    return os.environ.get(name, default)
"""

FAKE_ENV_LOADER = """
import os
_SNAPSHOT = {}
def _read(path):
    out = {}
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                if "=" in line:
                    name, value = line.strip().split("=", 1)
                    out[name] = value
    return out
def load_hermes_dotenv(*, hermes_home=None, **_):
    os.environ.update(_read(os.path.join(str(hermes_home), ".env")))
    os.environ.update(_read(os.path.join(str(hermes_home), "source.env")))
def hydrate_profile_secret_sources(hermes_home):
    _SNAPSHOT[str(hermes_home)] = _read(os.path.join(str(hermes_home), "source.env"))
    return dict(_SNAPSHOT[str(hermes_home)])
def get_secret_source_values(hermes_home):
    return dict(_SNAPSHOT.get(str(hermes_home), {}))
"""

# The settings Hermes still reads straight from os.environ, and two secrets it must not find there.
WATCHED = ("DEEPSEEK_BASE_URL", "FAKE_COMMAND", "FAKE_ARGS", "CLAUDE_CONFIG_DIR",
           "DEEPSEEK_API_KEY", "UNRELATED_TOKEN")

# Hermes's own split, in miniature: the key through get_secret, the routing settings straight from
# os.environ. It records what os.environ held for WATCHED in seen.json.
FAKE_RUNTIME = """
import json, os
from agent.secret_scope import get_secret
WATCHED = %r
def _get_model_config():
    return {"default": "m", "provider": get_secret("FAKE_REQUESTED")}
def resolve_requested_provider():
    return get_secret("FAKE_REQUESTED")
def resolve_runtime_provider(**_):
    with open(os.path.join(os.environ["HERMES_HOME"], "seen.json"), "w") as handle:
        json.dump({name: os.environ.get(name) for name in WATCHED}, handle)
    if get_secret("FAKE_FAIL"):
        raise RuntimeError("No usable credentials found for provider 'deepseek'.")
    return {"provider": "deepseek", "api_mode": "chat_completions",
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.test/v1"),
            "api_key": get_secret("DEEPSEEK_API_KEY", "")}
""" % (WATCHED,)

FAKE_AUTH = """
from types import SimpleNamespace
PROVIDER_REGISTRY = {"deepseek": SimpleNamespace(auth_type="api_key",
                                                 base_url_env_var="DEEPSEEK_BASE_URL")}
def resolve_provider(requested=None, **_):
    return {"ds": "deepseek"}.get(requested, requested)
"""

FAKE_PROVIDERS = """
from types import SimpleNamespace
def get_provider_profile(name):
    if name == "deepseek":
        return SimpleNamespace(process_command_env_vars=("FAKE_COMMAND",), process_args_env_var="FAKE_ARGS")
    return None
"""


def write(root: Path, files: dict) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        write(self.source, {"hermes_cli/__init__.py": "", "hermes_cli/env_loader.py": FAKE_ENV_LOADER,
                            "agent/__init__.py": "", "agent/secret_scope.py": FAKE_SCOPE})
        for patcher in (mock.patch.object(config, "home", return_value=self.home),
                        mock.patch.object(config, "profile_dir", return_value=self.home),
                        mock.patch.object(seat_model, "hermes_interpreter",
                                          return_value=(sys.executable, str(self.source)))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def secrets(self, dotenv: dict, external: dict | None = None) -> None:
        """The profile's ``.env``, and what its external source (a command helper) supplies."""
        for name, values in ((".env", dotenv), ("source.env", external or {})):
            (self.home / name).write_text("".join(f"{k}={v}\n" for k, v in values.items()))
            (self.home / name).chmod(0o600)


class Resolver(Base):
    def setUp(self):
        super().setUp()
        write(self.source, {"hermes_cli/runtime_provider.py": FAKE_RUNTIME,
                            "hermes_cli/auth.py": FAKE_AUTH, "providers/__init__.py": FAKE_PROVIDERS})

    def resolve(self) -> tuple[dict, dict]:
        """The resolver's answer, and what Hermes found in os.environ while it resolved."""
        answer = seat_model.run_resolver("rev", "resolve", {})
        seen = self.home / "seen.json"
        return answer, json.loads(seen.read_text()) if seen.exists() else {}

    def test_the_key_comes_from_the_scope_and_no_secret_enters_the_environment(self):
        # From the .env, and from a cold external source only (Vex's F1 on #2568): a fresh
        # process must resolve the source into the scope's snapshot itself.
        for where in (".env", "external source"):
            with self.subTest(where=where):
                secrets = {"DEEPSEEK_API_KEY": KEY, "UNRELATED_TOKEN": OTHER}
                if where == ".env":
                    self.secrets({"FAKE_REQUESTED": "deepseek", **secrets})
                else:
                    self.secrets({"FAKE_REQUESTED": "deepseek"}, secrets)
                answer, seen = self.resolve()
                self.assertNotIn("error", answer)
                self.assertEqual(answer["key"], KEY)
                self.assertEqual((seen["DEEPSEEK_API_KEY"], seen["UNRELATED_TOKEN"]), (None, None))

    def test_the_routing_settings_reach_hermes_and_no_secret_does(self):
        # Hermes reads these from os.environ, so the resolver copies exactly them from the scope,
        # for the requested provider and for the provider an alias names.
        routing = {"DEEPSEEK_BASE_URL": "https://ds.test/v1", "FAKE_COMMAND": "/opt/helper",
                   "FAKE_ARGS": "--fast", "CLAUDE_CONFIG_DIR": "/srv/claude"}
        for requested in ("deepseek", "ds"):
            for where in (".env", "external source"):
                with self.subTest(requested=requested, where=where):
                    secrets = {**routing, "DEEPSEEK_API_KEY": KEY, "UNRELATED_TOKEN": OTHER}
                    if where == ".env":
                        self.secrets({"FAKE_REQUESTED": requested, **secrets})
                    else:
                        self.secrets({"FAKE_REQUESTED": requested}, secrets)
                    answer, seen = self.resolve()
                    self.assertEqual(answer["base_url"], "https://ds.test/v1")
                    self.assertEqual(seen, {**routing, "DEEPSEEK_API_KEY": None, "UNRELATED_TOKEN": None})

    def test_no_secret_value_appears_in_the_resolvers_answer_or_diagnostics(self):
        # The key travels in the answer's ``key`` field, to the host proxy only; no other field,
        # and no failure, carries a secret value.
        self.secrets({"FAKE_REQUESTED": "deepseek", "DEEPSEEK_API_KEY": KEY, "UNRELATED_TOKEN": OTHER})
        answer, _ = self.resolve()
        public = json.dumps({k: v for k, v in answer.items() if k != "key"})
        self.assertNotIn(KEY, public)
        self.assertNotIn(OTHER, public)
        self.secrets({"FAKE_REQUESTED": "deepseek", "FAKE_FAIL": "1", "DEEPSEEK_API_KEY": KEY,
                      "UNRELATED_TOKEN": OTHER})
        answer, _ = self.resolve()
        self.assertEqual(answer["kind"], "credential")
        self.assertNotIn(KEY, json.dumps(answer))
        self.assertNotIn(OTHER, json.dumps(answer))


FAKE_CLIENT = """
import json, os
from types import SimpleNamespace
class Object:
    def __init__(self, value): self.value = value
    def model_dump(self): return self.value
class Client:
    def __init__(self, **kw):
        self.kw = kw
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **payload):
        seen = {"command": self.kw["command"], "config": self.kw["env"].get("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"),
                "leaked": sorted(k for k in os.environ if k in ("DEEPSEEK_API_KEY", "UNRELATED_TOKEN"))}
        return Object({"choices": [{"message": {"content": json.dumps(seen)}}]})
    def close(self): pass
"""


class DirectSDKChild(Base):
    def setUp(self):
        super().setUp()
        self.plugin = self.home / "plugins" / backend.PROVIDER
        self.plugin.mkdir(parents=True)
        (self.plugin / "directsdk.py").write_text(FAKE_CLIENT)
        self.native = self.root / "claude-config"
        self.native.mkdir()

    def test_the_child_reads_its_settings_from_the_scope_and_no_secret_enters_the_environment(self):
        settings = {"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/bin/true",
                    "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR": str(self.native),
                    "DEEPSEEK_API_KEY": KEY, "UNRELATED_TOKEN": OTHER}
        for where in (".env", "external source"):
            with self.subTest(where=where):
                if where == ".env":
                    self.secrets(settings)
                else:
                    self.secrets({}, settings)
                endpoint = backend.DirectSDKBackend("rev", {})
                self.addCleanup(endpoint.close)
                payload = {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "OK"}]}
                status, _, reply = endpoint.post(json.dumps(payload).encode(), {})
                self.assertEqual(status, 200)
                seen = json.loads(json.loads(b"".join(reply))["choices"][0]["message"]["content"])
                self.assertEqual(seen, {"command": "/bin/true", "config": str(self.native), "leaked": []})

    def test_a_failing_child_says_only_the_error_class(self):
        # What the child prints when it fails is the host's diagnostic; no secret value reaches it.
        self.secrets({"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/nonexistent/" + KEY,
                      "DEEPSEEK_API_KEY": KEY, "UNRELATED_TOKEN": OTHER})
        child = Path(backend.__file__).with_name("directsdk_child.py")
        run = subprocess.run([sys.executable, "-E", "-s", "-B", str(child), str(self.source), str(self.plugin)],
                             env={"HOME": str(self.root), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                  "HERMES_HOME": str(self.home), "TMPDIR": str(self.root)},
                             input=b"{}", capture_output=True, timeout=60, check=False)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stderr.decode().strip(), "ValueError")
        for value in (KEY, OTHER):
            self.assertNotIn(value.encode(), run.stdout + run.stderr)


@_home_guard.needs_real_hermes(reason="no Hermes checkout")
class RealHermes(unittest.TestCase):
    """The pinned Hermes resolves a seat whose key lives only in the profile's ``.env``, or only
    in a command helper (a cold external source), through the scope."""

    def resolve(self, cfg: dict, dotenv: str) -> dict:
        source = Path(_home_guard.HERMES_AGENT_SOURCE)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "hermes"
        profile = home / "profiles" / "seat"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(json.dumps(cfg))
        (profile / ".env").write_text(dotenv)
        settings = {"source": str(source), "venv": str(source / "venv"), "runtime": temp.name,
                    "rust": temp.name}
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            return seat_model.run_resolver("seat", "resolve", settings)

    def test_a_dotenv_key_and_base_url_resolve_through_the_scope(self):
        answer = self.resolve({"model": {"provider": "deepseek", "default": "deepseek-chat"}},
                              f"DEEPSEEK_API_KEY={KEY}\nDEEPSEEK_BASE_URL=https://ds.test/v1\n")
        self.assertNotIn("error", answer)
        self.assertEqual((answer["key"], answer["base_url"]), (KEY, "https://ds.test/v1"))

    def test_a_key_only_a_command_helper_supplies_resolves(self):
        helper = f'printf "DEEPSEEK_API_KEY={KEY}\\n"'
        answer = self.resolve({"model": {"provider": "deepseek", "default": "deepseek-chat"},
                               "secrets": {"command": {"enabled": True, "command": helper}}}, "")
        self.assertNotIn("error", answer)
        self.assertEqual(answer["key"], KEY)


if __name__ == "__main__":
    unittest.main()
