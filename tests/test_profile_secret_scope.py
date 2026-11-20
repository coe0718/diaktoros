"""#2374: the seat resolver and the DirectSDK child read a profile's secrets through Hermes's
``agent.secret_scope``, never by loading its ``.env`` into ``os.environ``.

A fake Hermes in a throwaway source tree: its ``load_hermes_dotenv`` loads the ``.env`` into
``os.environ`` as the real one does, so a run that still calls it shows up as a leaked key. No real
Hermes, credentials or network.
"""
import _home_guard
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from diaktoros import config, directsdk_backend as backend, seat_model

KEY = "profile-key-placeholder-2374"

FAKE_SCOPE = """
import os
_SCOPE = None
def build_profile_secret_scope(hermes_home):
    out = {}
    path = os.path.join(str(hermes_home), ".env")
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                if "=" in line:
                    name, value = line.strip().split("=", 1)
                    out[name] = value
    return out
def set_secret_scope(secrets, *, profile_home=None):
    global _SCOPE
    _SCOPE = dict(secrets)
def get_secret(name, default=None):
    if _SCOPE is not None and name in _SCOPE:
        return _SCOPE[name]
    return os.environ.get(name, default)
"""

FAKE_DOTENV = """
import os
def load_hermes_dotenv(*, hermes_home=None, **_):
    path = os.path.join(str(hermes_home), ".env")
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                if "=" in line:
                    name, value = line.strip().split("=", 1)
                    os.environ[name] = value
"""

# Hermes's own split, in miniature: the key through get_secret, the base-URL override straight
# from os.environ (as hermes_cli.auth_declared_providers.provider_env_base_url still reads it). A
# key that is also in os.environ comes back marked, so the answer shows the leak.
FAKE_RUNTIME = """
import os
from agent.secret_scope import get_secret
def _get_model_config():
    return {"default": "m", "provider": get_secret("FAKE_REQUESTED")}
def resolve_requested_provider():
    return get_secret("FAKE_REQUESTED")
def resolve_runtime_provider(**_):
    return {"provider": "deepseek", "api_mode": "chat_completions",
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.test/v1"),
            "api_key": get_secret("DEEPSEEK_API_KEY", "")
                       + (" (also in os.environ)" if "DEEPSEEK_API_KEY" in os.environ else "")}
"""

FAKE_AUTH = """
from types import SimpleNamespace
PROVIDER_REGISTRY = {"deepseek": SimpleNamespace(auth_type="api_key",
                                                 base_url_env_var="DEEPSEEK_BASE_URL")}
def resolve_provider(requested=None, **_):
    return {"ds": "deepseek"}.get(requested, requested)
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
        write(self.source, {"hermes_cli/__init__.py": "", "hermes_cli/env_loader.py": FAKE_DOTENV,
                            "agent/__init__.py": "", "agent/secret_scope.py": FAKE_SCOPE})
        for patcher in (mock.patch.object(config, "home", return_value=self.home),
                        mock.patch.object(config, "profile_dir", return_value=self.home),
                        mock.patch.object(seat_model, "hermes_interpreter",
                                          return_value=(sys.executable, str(self.source)))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def env_file(self, values: dict) -> None:
        (self.home / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        (self.home / ".env").chmod(0o600)


class Resolver(Base):
    def setUp(self):
        super().setUp()
        write(self.source, {"hermes_cli/runtime_provider.py": FAKE_RUNTIME,
                            "hermes_cli/auth.py": FAKE_AUTH})

    def resolve(self, requested: str) -> dict:
        self.env_file({"FAKE_REQUESTED": requested, "DEEPSEEK_API_KEY": KEY,
                       "DEEPSEEK_BASE_URL": "https://ds.test/v1"})
        return seat_model.run_resolver("rev", "resolve", {})

    def test_the_key_comes_from_the_scope_and_never_enters_the_environment(self):
        answer = self.resolve("deepseek")
        self.assertNotIn("error", answer)
        self.assertEqual(answer["key"], KEY)

    def test_the_providers_base_url_override_still_applies(self):
        # Hermes reads the base-URL override from os.environ, so the resolver copies that one
        # setting from the scope; an alias reaches its provider's override too.
        for requested in ("deepseek", "ds"):
            with self.subTest(requested=requested):
                self.assertEqual(self.resolve(requested)["base_url"], "https://ds.test/v1")


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
                "leaked": sorted(k for k in os.environ if k == "DEEPSEEK_API_KEY")}
        return Object({"choices": [{"message": {"content": json.dumps(seen)}}]})
    def close(self): pass
"""


class DirectSDKChild(Base):
    def test_the_child_reads_its_settings_from_the_scope_and_no_key_enters_the_environment(self):
        plugin = self.home / "plugins" / backend.PROVIDER
        plugin.mkdir(parents=True)
        (plugin / "directsdk.py").write_text(FAKE_CLIENT)
        native = self.root / "claude-config"
        native.mkdir()
        self.env_file({"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/bin/true",
                       "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR": str(native), "DEEPSEEK_API_KEY": KEY})
        endpoint = backend.DirectSDKBackend("rev", {})
        self.addCleanup(endpoint.close)
        payload = {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "OK"}]}
        status, _, reply = endpoint.post(json.dumps(payload).encode(), {})
        self.assertEqual(status, 200)
        seen = json.loads(json.loads(b"".join(reply))["choices"][0]["message"]["content"])
        self.assertEqual(seen["command"], "/bin/true")
        self.assertEqual(seen["config"], str(native))
        self.assertEqual(seen["leaked"], [])


@_home_guard.needs_real_hermes(reason="no Hermes checkout")
class RealHermes(unittest.TestCase):
    """The pinned Hermes resolves a seat whose key and base-URL override live only in the
    profile's ``.env``, through the scope."""

    def test_a_dotenv_key_and_base_url_resolve_through_the_scope(self):
        source = Path(_home_guard.HERMES_AGENT_SOURCE)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name) / "hermes"
        profile = home / "profiles" / "seat"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(json.dumps(
            {"model": {"provider": "deepseek", "default": "deepseek-chat"}}))
        (profile / ".env").write_text(f"DEEPSEEK_API_KEY={KEY}\nDEEPSEEK_BASE_URL=https://ds.test/v1\n")
        settings = {"source": str(source), "venv": str(source / "venv"), "runtime": temp.name,
                    "rust": temp.name}
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            answer = seat_model.run_resolver("seat", "resolve", settings)
        self.assertNotIn("error", answer)
        self.assertEqual((answer["key"], answer["base_url"]), (KEY, "https://ds.test/v1"))


if __name__ == "__main__":
    unittest.main()
