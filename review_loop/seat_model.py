"""Which model, provider and credential a seat's isolated turn uses (issue #32).

Each seat — reviewer, fixer, adjudicator — runs as its own Hermes profile, and the profile decides
the model, the provider and the account. The isolated turn cannot read the profile itself (its
sandbox has no host home and no credentials), so the **host** resolves it, right before the turn:

* the seat's profile home (``$HERMES_HOME`` for ``default``, else ``profiles/<name>``) is handed to
  Hermes's own resolution — ``hermes_cli.env_loader.load_hermes_dotenv`` for the profile's
  ``.env`` and secret sources, then ``hermes_cli.runtime_provider.resolve_runtime_provider`` — the
  same path ``hermes -p <name> chat`` takes, including ``auth.json`` credential pools and named
  ``custom_providers``. It runs in a **separate process per seat**, with an environment built from
  scratch (no inherited provider variables), so one seat's credential can never satisfy another
  seat's resolution. The key comes back over a pipe and lives only in the host inference proxy.
* the result must be one of the inference proxy's wire contracts — ``chat_completions``,
  ``codex_responses`` or ``anthropic_messages`` (see ``inference_proxy.CONTRACTS``) — over HTTPS
  with a non-empty credential. API-key providers and the OAuth/subscription providers Hermes
  resolves to one of those (``openai-codex`` and ``xai-oauth`` → Responses, ``qwen-oauth`` and
  ``nous`` → chat-completions, ``minimax-oauth`` → Messages with a bearer token, ``anthropic``
  with a Claude subscription token → Messages with the Claude Code identity) are accepted.
  Copilot, Bedrock, Vertex, Azure Foundry, MoA, the ``codex_app_server`` runtime and any other
  ``api_mode`` are refused *before* a credential is looked up.
* an OAuth credential is only its short-lived **access token**: Hermes keeps the refresh token
  in the profile's ``auth.json`` (under its own ``auth.lock``). The host re-runs the same
  isolated resolution when the token nears the expiry Hermes (or the token itself) states, or
  once after an upstream 401; every resolution of one profile is serialized (a per-profile
  thread lock plus a ``flock``), so two seats sharing a profile never refresh in parallel, and
  the second one simply reads the token the first refreshed. The sandbox only ever sees a dummy.

``review-loop-runtime.json`` names host paths (``source``, ``venv``, ``runtime``, ``rust``). The
model may additionally be overridden there. Precedence, per seat:

1. ``seats.<seat>`` in the runtime file — ``{"model", "upstream", "key_file"}`` — an explicit,
   per-seat override (testing, or a profile whose provider the proxy cannot speak);
2. the seat's Hermes profile — the default, and the point of the design;
3. the legacy top-level ``model``/``upstream``/``key_file`` — used **only** when the profile cannot
   be resolved, so pre-#32 runtime files keep working; ``doctor`` and ``selftest`` warn whenever
   it is in effect, because it gives every such seat the same model.

Anything else fails closed: the turn is not launched and the ledger records the reason.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from . import config, hostdirs, util

SEATS = ("reviewer", "fixer", "adjudicator")
HOST_KEYS = ("source", "venv", "runtime", "rust")
OVERRIDE_KEYS = ("model", "upstream", "key_file")
RESOLVE_TIMEOUT = 90

# Providers refused by name before Hermes resolves (and possibly refreshes) anything: a token
# exchange with provider-specific client headers (Copilot), cloud IAM signing (Bedrock, Vertex,
# Azure Foundry), or a fan-out of several models (MoA).
UNSUPPORTED_PROVIDERS = frozenset({
    "copilot", "copilot-acp", "github-copilot", "bedrock", "aws-bedrock", "vertex",
    "google-vertex", "vertex-ai", "gcp-vertex", "vertexai", "azure-foundry", "moa",
})
# OAuth/subscription providers whose Hermes runtime speaks a proxied wire format, and that format.
# ``anthropic`` is not listed: it may hold an API key or a Claude subscription token, and Hermes's
# resolution decides (``client_identity == "claude_code"`` when it is the subscription).
OAUTH_PROVIDERS = {"openai-codex": "codex_responses", "xai-oauth": "codex_responses",
                   "qwen-oauth": "chat_completions", "nous": "chat_completions",
                   "minimax-oauth": "anthropic_messages"}
ANTHROPIC_ALIASES = frozenset({"anthropic", "claude", "claude-code"})
REFUSED_API_MODES = frozenset({"bedrock_converse", "codex_app_server"})

# Optional Hermes extras a seat's sandboxed Hermes imports to talk to its provider (#118). Keyed
# by the extra's name in Hermes's ``pyproject.toml`` ``[project.optional-dependencies]``; every
# fact is from the pinned Hermes source. The sandbox runs on the wire (``api_mode``) the host's
# Hermes resolves for the seat, so the table predicts that resolution:
#
# * ``module`` — the import that proves the extra is installed (Hermes's own anchor for it,
#   ``pm/extras.py`` ``ANCHORS``), checked with ``find_spec`` by the runtime venv's python;
# * ``wires`` — the proxied ``api_mode``s whose Hermes client imports it (``anthropic_messages``:
#   ``agent/agent_init.py`` → ``anthropic_adapter.build_anthropic_client`` → ``import anthropic``,
#   for every provider on that wire, the Claude subscription included);
# * ``pinned`` — providers whose wire Hermes fixes whatever ``model.api_mode`` says:
#   ``anthropic`` (``runtime_provider``: ``provider == "anthropic"`` → ``anthropic_messages``) and
#   ``minimax-oauth`` (``_minimax_oauth_runtime``);
# * ``providers`` / ``hosts`` / ``url_suffixes`` / ``host_paths`` — providers whose overlay
#   transport is that wire (``hermes_cli/providers.py``) and base URLs Hermes maps onto it
#   (``runtime_provider._detect_api_mode_for_url``). These are *fallbacks*: a configured
#   ``model.api_mode`` wins over both (``_configured_or_fallback_api_mode``: configured, else
#   ``_fallback_api_mode``; ``_custom_runtime``: ``api_mode or _detect_api_mode_for_url``), so an
#   explicit non-Messages ``api_mode`` means the package is never imported;
# * ``models`` — provider families whose *model* picks the wire, by model-id prefix
#   (``hermes_cli/models.py`` ``_OPENCODE_API_MODE_PREFIXES``). For the built-in providers
#   (``builtin_models``) the model wins over a configured ``api_mode`` (``opencode_by_model``); a
#   custom provider named after a family keeps its own ``api_mode``;
# * ``possible`` — providers Hermes *can* put on that wire depending on what doctor does not read:
#   ``models`` prefixes (empty: any model), ``required_if`` / ``possible_if`` the profile settings
#   that make it certain / possible (neither matching: never), ``unless_configured`` when an
#   explicit ``base_url`` or ``api_mode`` decides instead, ``ignores_api_mode`` when Hermes derives
#   the wire itself (``nous``: ``nous_api_mode``), and ``why``. Without the package such a
#   seat is ⚠️, never ✅.
#
# The fix is Hermes's own command for a missing extra (``pm/extras.py`` ``install_hint``).
HERMES_EXTRAS = {
    "anthropic": {
        "module": "anthropic",
        "wires": frozenset({"anthropic_messages"}),
        "pinned": ANTHROPIC_ALIASES | {"minimax-oauth"},
        "providers": frozenset({"minimax", "minimax-cn", "tencent-tokenplan"}),
        "hosts": frozenset({"api.anthropic.com"}),
        "url_suffixes": ("/anthropic", "/anthropic/v1"),
        "host_paths": (("api.kimi.com", "/coding"),),
        "models": {"opencode-zen": ("claude-", "union-alpha", "qwen"),
                   "opencode-go": ("minimax-", "qwen", "union-alpha")},
        "builtin_models": frozenset({"opencode-zen", "opencode", "zen", "opencode-go", "go",
                                     "opencode-go-sub"}),
        "possible": {
            # hermes_cli/providers.py nous_api_mode: anthropic/* is on the Messages wire only with
            # nous.anthropic_wire: native; agent/nous_wire.py may promote a session to it only on
            # `auto`. Unset or `chat` never reaches it. nous ignores model.api_mode.
            "nous": {"models": ("anthropic/",),
                     "required_if": {"nous_anthropic_wire": "native"},
                     "possible_if": {"nous_anthropic_wire": "auto"}, "ignores_api_mode": True,
                     "why": "when nous.anthropic_wire: auto promotes the session"},
            # hermes_cli/auth_zai_kimi.py _resolve_kimi_base_url: a Kimi Code (sk-kimi-) key is
            # redirected to api.kimi.com/coding, which Hermes speaks on the Messages wire.
            "kimi-coding": {"models": (), "unless_configured": True,
                            "why": "with a Kimi Code key (redirected to api.kimi.com/coding)"},
        },
    },
}
# Hermes's aliases for the provider families above (hermes_cli/auth.py, hermes_cli/models.py
# opencode_provider_family), so a profile's spelling finds its table entry.
PROVIDER_FAMILIES = {
    "opencode": "opencode-zen", "zen": "opencode-zen", "go": "opencode-go",
    "opencode-go-sub": "opencode-go",
    "nous-portal": "nous", "nousresearch": "nous",
    "kimi": "kimi-coding", "kimi-for-coding": "kimi-coding", "moonshot": "kimi-coding",
    "kimi-coding-cn": "kimi-coding", "kimi-cn": "kimi-coding", "moonshot-cn": "kimi-coding",
}
# Hermes's spellings of a configured api_mode (hermes_cli/config_providers.py _API_MODE_ALIASES,
# all 13). Used only when Hermes itself cannot be imported: the describe step asks Hermes's own
# ``runtime_provider._parse_api_mode`` whenever it can.
API_MODE_ALIASES = {
    "openai": "chat_completions", "openai_chat": "chat_completions",
    "openai-chat": "chat_completions", "chat-completions": "chat_completions",
    "chatcompletions": "chat_completions", "responses": "codex_responses",
    "openai_responses": "codex_responses", "openai-responses": "codex_responses",
    "anthropic": "anthropic_messages", "anthropic-messages": "anthropic_messages",
    "messages": "anthropic_messages", "bedrock": "bedrock_converse",
    "bedrock-converse": "bedrock_converse",
}


def provider_family(provider: str) -> str:
    provider = (provider or "").strip().lower()
    for family in ("opencode-go", "opencode-zen"):    # built-ins and custom names extending them
        if provider.startswith(family):
            return family
    return PROVIDER_FAMILIES.get(provider, provider)


def _bare_model(provider: str, family: str, model: str) -> str:
    model = (model or "").strip().lower()
    for prefix in (f"{provider}/", f"{family}/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _matches(facts: dict, wanted: dict) -> bool:
    return bool(wanted) and all(str(facts.get(k) or "").strip().lower() == v
                                for k, v in wanted.items())


def _canonical_mode(raw: str) -> str:
    """A configured api_mode as Hermes reads it (``runtime_provider._parse_api_mode``): aliases
    canonicalized, anything unknown ignored."""
    mode = (raw or "").strip().lower()
    mode = API_MODE_ALIASES.get(mode, mode)
    return mode if mode in {"chat_completions", "codex_responses", "anthropic_messages",
                            "bedrock_converse", "codex_app_server"} else ""


def _url_wire(spec: dict, url: str) -> bool:
    """Without Hermes: does this base URL map onto the extra's wire? Mirrors
    ``runtime_provider._detect_api_mode_for_url`` — the *parsed path* is tested, so a query or
    fragment does not hide an ``/anthropic`` endpoint. With Hermes, its own function answers."""
    raw = (url or "").strip().lower()
    parts = urlsplit(raw)
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    return bool(raw) and (host in spec["hosts"] or path.endswith(spec["url_suffixes"])
                          or any(host == h and p in path for h, p in spec["host_paths"]))


def _opencode_family(provider: str, url: str) -> str:
    """``runtime_provider_custom._opencode_family_for_custom``: by name, else an opencode.ai host."""
    family = provider_family(provider)
    if family in ("opencode-zen", "opencode-go"):
        return family
    url = (url or "").strip().lower()
    if (urlsplit(url).hostname or "") == "opencode.ai":
        return "opencode-go" if "/zen/go" in url else "opencode-zen"
    return ""


def _named_entry_extras(provider: str, model: str, entry: dict,
                        explain: dict) -> tuple[list[str], list[str]]:
    """A named custom provider: Hermes ignores ``model.api_mode`` and takes the wire from the
    entry — its ``api_mode``/``transport``, else URL detection, else chat
    (``_custom_runtime``) — except that an OpenCode-family entry with no ``api_mode`` re-derives
    it from the model on the direct path while a credential-pool hit keeps the URL's wire
    (``_try_resolve_from_custom_pool`` returns first). Doctor cannot see the pool, so when the two
    paths disagree the extra is only *possible*.

    ``entry`` comes from Hermes's own lookup when Hermes is importable (``url_wire``,
    ``family`` and ``model_wire`` computed by Hermes); the local fallbacks below only fill in a
    field it did not supply."""
    where = entry.get("where") or "the provider entry"
    pinned = _canonical_mode(str(entry.get("api_mode") or ""))
    url = str(entry.get("url") or "")
    effective = str(entry.get("model") or model or "")
    family = entry["family"] if "family" in entry else _opencode_family(provider, url)
    required, possible = [], []
    for extra, spec in HERMES_EXTRAS.items():
        wire = sorted(spec["wires"])[0]
        if pinned:
            if pinned in spec["wires"]:
                required.append(extra)
                explain[extra] = f"{where} sets api_mode {pinned}"
            explain["wire"] = pinned
            continue
        by_url = (entry["url_wire"] in spec["wires"] if "url_wire" in entry
                  else _url_wire(spec, url))
        if "model_wire" in entry:
            by_model = bool(family) and entry["model_wire"] in spec["wires"]
        else:
            bare = _bare_model(provider, family, effective) if family else ""
            by_model = bool(family) and bare.startswith(spec["models"].get(family, ()))
        if by_url and (by_model or not family):
            required.append(extra)
            explain[extra] = f"Hermes maps {where}'s URL to {wire}"
            explain["wire"] = wire
        elif by_url or by_model:
            possible.append(extra)
            explain[extra] = (f"{where} is an OpenCode-family endpoint: Hermes derives the wire "
                              f"from the model ({effective}) unless its credential comes from a "
                              "pool, which keeps the URL's wire")
            explain["wire"] = (entry.get("model_wire") if family and entry.get("model_wire")
                               else wire if by_model else "")
        else:
            explain["wire"] = ((entry.get("model_wire") if family else "")
                               or entry.get("url_wire") or "chat_completions")
    return required, possible


def extras_for(provider: str, api_mode: str = "", base_url: str = "", *, model: str = "",
               configured: str = "", facts: dict | None = None, entry: dict | None = None,
               hermes: dict | None = None, entry_hint: bool = False,
               explain: dict | None = None) -> tuple[list[str], list[str]]:
    """``(required, possible)`` optional Hermes extras (``HERMES_EXTRAS`` keys) for a seat.

    ``api_mode`` is the wire the seat is expected on; ``configured`` the profile's own
    ``model.api_mode`` (empty when unset), which beats provider and URL fallbacks as it does in
    Hermes; ``facts`` carries profile settings ``required_if``/``possible_if`` read
    (``nous_anthropic_wire``) — never a credential. ``entry`` is the named custom provider the
    profile selects (``{"where", "url", "api_mode", "model"}``, from the describe step): Hermes
    resolves such a seat from the entry, not from ``model.api_mode``. ``explain``, when given,
    receives the reason per extra and ``"wire"`` — the wire Hermes will use when it is decided.
    ``hermes`` is the describe step's answer from Hermes's own functions (``url_wire`` for the
    model's base URL, ``entry``); ``None`` means Hermes could not be imported there, and then
    ``entry_hint`` — a config entry *might* decide this provider — makes anything not required
    only *possible*: without Hermes, doctor does not claim "not needed" for such a seat.
    Raises ValueError on a malformed URL.
    """
    explain = explain if explain is not None else {}
    provider = (provider or "").strip().lower()
    entry = entry or (hermes or {}).get("entry")
    if entry:
        return _named_entry_extras(provider, model, entry, explain)
    family = provider_family(provider)
    bare = _bare_model(provider, family, model)
    url = (base_url or "").strip().rstrip("/").lower()
    urlsplit(url).hostname                              # a malformed URL raises here
    configured = _canonical_mode(configured)
    facts = facts or {}
    required, possible = [], []
    for extra, spec in HERMES_EXTRAS.items():
        maybe = spec["possible"].get(family)
        wire = sorted(spec["wires"])[0]
        # Hermes derives some providers' wire itself (nous), whatever model.api_mode says.
        mode, conf = ("", "") if maybe and maybe.get("ignores_api_mode") else (api_mode, configured)
        overridden = bool(conf) and conf not in spec["wires"]
        by_model = (family in spec["models"] and bare.startswith(spec["models"][family])
                    and (provider in spec["builtin_models"] or not overridden))
        reason = (f"{provider} is always on {wire}" if provider in spec["pinned"]
                  else f"model.api_mode is {conf}" if conf in spec["wires"]
                  else f"{provider}/{model} is routed to {wire}" if by_model
                  else f"{provider} is on {mode}" if mode in spec["wires"]
                  else f"{provider}'s default wire is {wire}"
                  if provider in spec["providers"] and not overridden
                  else f"Hermes maps {url} to {wire}" if not overridden and (
                      hermes.get("url_wire") in spec["wires"] if hermes is not None
                      else _url_wire(spec, url))
                  else "")
        if reason:
            required.append(extra)
            explain[extra], explain["wire"] = reason, wire
            continue
        if (not maybe or (maybe["models"] and not bare.startswith(maybe["models"]))
                or (maybe.get("unless_configured") and (url or overridden))):
            continue
        if _matches(facts, maybe.get("required_if") or {}):
            required.append(extra)
            setting = ", ".join(f"{k.replace('_', '.', 1)}: {v}"
                                for k, v in maybe["required_if"].items())
            explain[extra], explain["wire"] = f"{setting} puts {model} on {wire}", wire
        elif "possible_if" not in maybe or _matches(facts, maybe["possible_if"]):
            possible.append(extra)
            explain[extra] = maybe["why"]
    if hermes is None and entry_hint:
        for extra in HERMES_EXTRAS:
            if extra not in required + possible:
                possible.append(extra)
                explain[extra] = ("when its providers:/custom_providers: entry puts it there — "
                                  "Hermes could not be imported from the runtime source to say")
    return required, possible


_SECRETISH = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
                        r"|sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|[A-Za-z0-9_-]{40,})")


class SeatModelError(Exception):
    """The seat's model cannot be resolved; the message says why and what to change."""


@dataclass(frozen=True)
class SeatInference:
    """Everything the host needs to start one seat's inference proxy. ``key`` never prints."""
    seat: str
    profile: str
    origin: str          # "override" | "profile" | "legacy"
    provider: str
    model: str
    upstream: str
    key: str = field(repr=False, compare=False)
    warning: str = ""
    api_mode: str = "chat_completions"
    auth: str = "api_key"                 # "api_key" | "oauth"
    scheme: str = "bearer"                # how the upstream takes it: "bearer" | "x-api-key"
    headers: tuple = field(default=(), repr=False)   # host-chosen, non-credential
    expires_at: float | None = field(default=None, repr=False, compare=False)
    wire_model: str = ""                  # the model id on the wire, when Hermes normalizes it
    client_identity: str = ""             # "claude_code" for a Claude subscription token
    refreshable: bool = False
    settings: dict | None = field(default=None, repr=False, compare=False)

    @property
    def host(self) -> str:
        return urlsplit(self.upstream).hostname or ""

    @property
    def proxy_model(self) -> str:
        """The model the proxy forces into every body (Hermes's wire spelling of it)."""
        return self.wire_model or self.model

    @property
    def auth_label(self) -> str:
        return "OAuth (host-refreshed)" if self.auth == "oauth" else "API key"

    def identity(self) -> tuple:
        """Distinct (provider, model, endpoint, credential) — for once-per-resolution checks."""
        return (self.provider, self.model, self.upstream,
                hashlib.sha256(self.key.encode()).hexdigest())

    def describe(self) -> str:
        where = {"override": "runtime override seats." + self.seat,
                 "legacy": "LEGACY runtime model",
                 "profile": "profile " + self.profile}[self.origin]
        what = self.model if self.origin != "profile" else f"{self.provider} / {self.model}"
        return f"{where}: {what} via {self.host} [{self.api_mode}, {self.auth_label}]"

    def credential(self):
        """The first ``inference_proxy.Credential`` (host-side only)."""
        from .inference_proxy import Credential
        return Credential(self.key, self.scheme, self.headers, self.expires_at)

    def credential_provider(self):
        """What the turn's proxy authenticates with: a static key, or a host-refreshed token."""
        from .inference_proxy import RefreshingCredential, StaticCredential
        if not self.refreshable or self.origin != "profile":
            return StaticCredential(self.credential())
        return RefreshingCredential(self.credential(), self._refresh)

    def _refresh(self, stale: str):
        """Re-resolve this seat's profile through Hermes; a new credential or an exception."""
        fresh = resolve_profile(self.profile, self.seat, self.settings,
                                stale=hashlib.sha256(stale.encode()).hexdigest())
        if (fresh.api_mode, fresh.upstream, fresh.wire_model or fresh.model) != \
                (self.api_mode, self.upstream, self.wire_model or self.model):
            raise SeatModelError(f"profile {self.profile} changed provider or model mid-turn")
        return fresh.credential()


# -- the runtime file --------------------------------------------------------------------------

def parse_runtime(settings: object) -> dict:
    """Validate the runtime file's shape; raise ``ValueError`` naming what is wrong.

    Required: the four host paths. Optional: the legacy global trio (all three or none) and a
    ``seats`` object of per-seat override trios.
    """
    if not isinstance(settings, dict):
        raise ValueError("runtime file must be a JSON object")
    allowed = set(HOST_KEYS) | set(OVERRIDE_KEYS) | {"seats"}
    extra = sorted(set(settings) - allowed)
    missing = [key for key in HOST_KEYS if key not in settings]
    if extra or missing:
        raise ValueError("runtime keys: " + "; ".join(
            ([f"missing {', '.join(missing)}"] if missing else []) +
            ([f"unexpected {', '.join(extra)}"] if extra else [])))
    bad = [key for key in HOST_KEYS if not isinstance(settings[key], str) or not settings[key]]
    if bad:
        raise ValueError(f"empty or non-string: {', '.join(bad)}")
    present = [key for key in OVERRIDE_KEYS if key in settings]
    if present and len(present) != len(OVERRIDE_KEYS):
        raise ValueError("the legacy model override needs all of model, upstream, key_file "
                         f"(found only {', '.join(present)})")
    if present:
        _override_shape(settings, "runtime")
    seats = settings.get("seats", {})
    if not isinstance(seats, dict):
        raise ValueError("seats must be an object of per-seat overrides")
    for seat, block in seats.items():
        if seat not in SEATS:
            raise ValueError(f"seats.{seat}: unknown seat (use {', '.join(SEATS)})")
        if not isinstance(block, dict) or set(block) != set(OVERRIDE_KEYS):
            raise ValueError(f"seats.{seat} must have exactly model, upstream, key_file")
        _override_shape(block, f"seats.{seat}")
    return settings


def _override_shape(block: dict, where: str) -> None:
    bad = [key for key in OVERRIDE_KEYS if not isinstance(block.get(key), str) or not block[key]]
    if bad:
        raise ValueError(f"{where}: empty or non-string {', '.join(bad)}")


def load_runtime(path: Path) -> dict:
    return parse_runtime(json.loads(Path(path).read_text()))


def legacy_override(settings: dict) -> dict | None:
    return ({key: settings[key] for key in OVERRIDE_KEYS}
            if all(key in settings for key in OVERRIDE_KEYS) else None)


def seat_override(settings: dict, seat: str) -> dict | None:
    return (settings.get("seats") or {}).get(seat)


def check_upstream(upstream: str, where: str, api_mode: str = "chat_completions") -> str:
    from .inference_proxy import _NoRedirectConnection, contract_for
    suffix = contract_for(api_mode).upstream_suffix
    if urlsplit(upstream).scheme != "https":
        raise SeatModelError(f"{where}: inference must use HTTPS, got {upstream.split(':', 1)[0]!r}")
    try:
        _NoRedirectConnection(upstream, suffix)
    except ValueError:
        raise SeatModelError(f"{where}: upstream must be https://host[:port]/…{suffix} "
                             "with no credentials, query or fragment") from None
    return upstream


def upstream_for(base_url: str, where: str, api_mode: str = "chat_completions") -> str:
    """A provider base URL → the proxy's fixed upstream URL for ``api_mode``.

    ``…/v1`` + ``/chat/completions``; a Responses base (``…/backend-api/codex``, ``…/v1``) +
    ``/responses``; an Anthropic base with any trailing ``/v1`` removed + ``/v1/messages`` (the
    Anthropic SDK's own rule).
    """
    from .inference_proxy import contract_for
    suffix = contract_for(api_mode).upstream_suffix
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise SeatModelError(f"{where}: the provider resolved without a base URL")
    if api_mode == "anthropic_messages":
        base = re.sub(r"/v1$", "", base)
    return check_upstream(base if base.endswith(suffix) else base + suffix, where, api_mode)


def _read_key_file(raw: str, where: str) -> str:
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
    except OSError:
        raise SeatModelError(f"{where}: key file {path} does not exist") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SeatModelError(f"{where}: key file {path} must be a regular 0600 file you own "
                             f"(`chmod 600 {path}`)")
    key = path.read_text().strip()
    if not key or "\n" in key or "\r" in key:
        raise SeatModelError(f"{where}: key file {path} is empty or has more than one line")
    return key


def _from_override(block: dict, seat: str, profile: str, origin: str, where: str,
                   warning: str = "") -> SeatInference:
    upstream = check_upstream(block["upstream"], where)
    return SeatInference(seat, profile, origin, "runtime", block["model"], upstream,
                         _read_key_file(block["key_file"], where), warning)


# -- Hermes, run as the seat's profile ------------------------------------------------------------

_RESOLVER = r'''
import base64, hashlib, json, os, sys, time
out = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)                      # anything Hermes prints goes to stderr, never into our answer
sys.stdout = sys.stderr
source, mode, policy = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
unsupported, oauth_providers = set(policy["unsupported"]), dict(policy["oauth"])
modes, refused_modes = set(policy["modes"]), set(policy["refused_modes"])
anthropic_aliases, stale = set(policy["anthropic"]), policy.get("stale") or ""
sys.path.insert(0, source)

def done(**answer):
    out.write(json.dumps(answer))
    out.flush()
    os._exit(0)

def text(exc):
    return (type(exc).__name__ + ": " + str(exc))[:400]

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else ""

def jwt_exp(token):
    """A JWT access token's own ``exp`` — only a refresh schedule hint, never trusted for auth."""
    try:
        part = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
        return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    except Exception:
        return None

def expiry(runtime, key):
    for name, scale in (("expires_at", 1.0), ("agent_key_expires_at", 1.0), ("expires_at_ms", 0.001)):
        value = runtime.get(name)
        if isinstance(value, bool) or value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            return float(value) * scale
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return jwt_exp(key)


class NoYamlReader(Exception):
    """No YAML reader in this interpreter: the config cannot be read at all, and calling it
    "unreadable" would blame a file that is perfectly fine."""


def read_config(raw):
    """Parse a config.yaml with whichever reader this interpreter has.

    Hermes reads its configs with ruamel.yaml, and the interpreter running this check was chosen
    by the host — on a packaged install that is Hermes's own bundled python, which ships neither
    PyYAML nor ruamel. Try both readers, then JSON (a JSON config is valid YAML), and when there
    is no reader at all report *that*: the profile's config being readable is exactly what made
    this failure look like a corrupt file.
    """
    for name in ("yaml", "ruamel.yaml"):
        try:
            if name == "yaml":
                import yaml as module
                loaded = module.safe_load(raw)
            else:
                from ruamel.yaml import YAML
                loaded = YAML(typ="safe").load(raw)
        except ImportError:
            continue
        return loaded or {}
    try:
        return json.loads(raw)
    except ValueError:
        raise NoYamlReader("this interpreter (" + sys.executable + ") has no YAML library "
                           "(looked for yaml and ruamel.yaml), so it cannot read a profile's "
                           "config at all; name a venv with Hermes's own dependencies in "
                           "$HERMES_HOME/review-loop-runtime.json (docs/configuration.md)") from None



# What the describe step needs from Hermes. Checked by name before any is called, so a pin that
# moves one reports which, instead of the check quietly deciding without Hermes.
HERMES_WIRE_FUNCTIONS = (
    ("hermes_cli.runtime_provider", "_parse_api_mode"),
    ("hermes_cli.runtime_provider", "_detect_api_mode_for_url"),
    ("hermes_cli.runtime_provider_custom", "_get_named_custom_provider"),
    ("hermes_cli.runtime_provider_custom", "_opencode_family_for_custom"),
    ("hermes_cli.runtime_provider_custom", "get_secret_str"),
    ("hermes_cli.models", "opencode_model_api_mode"),
    ("hermes_cli.auth", "resolve_provider"),
)


def hermes_facts(requested, block):
    """Hermes's own answers; ``{"unavailable": "missing"|"drift", "detail"}`` when Hermes could
    not be asked (not importable, or lacking a function doctor needs) — never a silent ``None``,
    the caller says so on both lines; or ``{"error"}`` when Hermes was asked and raised, which
    holds the seat exactly as the turn's own resolution would."""
    import importlib
    try:
        import hermes_cli  # noqa: F401
    except Exception as exc:
        return {"unavailable": "missing",
                "detail": "Hermes is not importable from " + source + " (" + text(exc) + ")"}
    missing = []
    for module, name in HERMES_WIRE_FUNCTIONS:
        try:
            if not callable(getattr(importlib.import_module(module), name, None)):
                missing.append(module + "." + name)
        except Exception as exc:
            missing.append(module + " (" + text(exc) + ")")
    if missing:
        return {"unavailable": "drift",
                "detail": "the Hermes at " + source + " lacks " + ", ".join(missing)}
    try:
        return _hermes_facts(requested, block)
    except Exception as exc:
        # Hermes WAS asked and raised on this profile: the turn resolves through the same code
        # and would fail the same way, so this is a refusal (the seat is held), not "not asked".
        return {"error": "the Hermes at " + source + " raised reading this profile ("
                         + text(exc) + ")"}


def _hermes_facts(requested, block):
    """Hermes's OWN answers for the wire questions doctor needs. Read-only and credential-free:
    ``load_config`` only reads config.yaml, and the named-provider lookup's one secret read (a
    ``key_env``) is answered with "" before it runs. Only endpoint and wire facts leave this
    process — never a key."""
    from hermes_cli import runtime_provider as rp
    from hermes_cli import runtime_provider_custom as rpc
    from hermes_cli.models import opencode_model_api_mode
    rpc.get_secret_str = lambda name, default="": default     # never read a credential
    facts = {"configured": rp._parse_api_mode(block.get("api_mode")) or ""}
    try:
        facts["url_wire"] = rp._detect_api_mode_for_url(str(block.get("base_url") or "")) or ""
    except Exception as exc:        # Hermes itself cannot read it (a named entry ignores it)
        facts["url_wire"], facts["url_error"] = "", text(exc)
    name = requested.strip().lower()
    if not name or name == "auto":
        return facts
    try:
        entry = rpc._get_named_custom_provider(name)
    except Exception as exc:
        facts["error"] = "Hermes refuses provider " + name + " (" + text(exc) + ")"
        return facts
    if entry:
        url = str(entry.get("base_url") or "").rstrip("/")
        api_mode = str(entry.get("api_mode") or "")
        model = str(entry.get("model") or block.get("default") or block.get("model") or "")
        family = rpc._opencode_family_for_custom(name, url) or ""
        key = str(entry.get("provider_key") or "")
        cfg = rp.load_config()
        where = ("providers." + key if key and isinstance(cfg.get("providers"), dict)
                 and key in cfg["providers"] else "custom_providers[" + str(entry.get("name")) + "]")
        facts["entry"] = {"where": where, "url": url, "api_mode": api_mode, "model": model,
                          "url_wire": rp._detect_api_mode_for_url(url) or "", "family": family,
                          "model_wire": (opencode_model_api_mode(family, model)
                                         if family and not api_mode and model else "")}
        return facts
    if name != "custom":
        try:
            from hermes_cli import auth as hermes_auth
            hermes_auth.resolve_provider(name)
        except Exception as exc:
            facts["error"] = ("Hermes knows no provider " + name + " (no enabled providers." + name
                              + " or custom_providers entry, and not a built-in: " + text(exc) + ")")
    return facts


def entry_hint(requested, cfg):
    """Without Hermes: might a providers:/custom_providers: entry decide this provider? A loose
    test on purpose — it only turns a verdict into "possible", never into "not needed"."""
    name = requested.strip().lower().replace(" ", "-")
    if not name or name == "auto":
        return False
    bare = name.split(":", 1)[1] if name.startswith("custom:") else name
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    legacy = cfg.get("custom_providers") if isinstance(cfg.get("custom_providers"), list) else []
    names = {str(k).strip().lower().replace(" ", "-") for k in providers}
    for entry in list(providers.values()) + legacy:
        if isinstance(entry, dict):
            names.update(str(entry.get(k) or "").strip().lower().replace(" ", "-")
                         for k in ("name", "provider_key"))
    return bare in names or name in names


home = os.environ["HERMES_HOME"]
if mode == "describe":             # read-only: the profile's config, no credential lookup
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as handle:
            raw = handle.read()
        cfg = read_config(raw)
    except NoYamlReader as exc:
        done(kind="interpreter", error=str(exc))
    except Exception as exc:
        done(kind="config", error="profile config.yaml unreadable (" + text(exc) + ")")
    block = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(block, str):
        block = {"default": block}
    block = block if isinstance(block, dict) else {}
    nous = cfg.get("nous") if isinstance(cfg, dict) else None
    nous = nous if isinstance(nous, dict) else {}
    done(hermes=hermes_facts(str(block.get("provider") or ""), block),
         entry_hint=entry_hint(str(block.get("provider") or ""), cfg if isinstance(cfg, dict) else {}),
         model=str(block.get("default") or block.get("model") or ""),
         requested=str(block.get("provider") or "auto").strip().lower(),
         base_url=str(block.get("base_url") or ""),
         api_mode=str(block.get("api_mode") or "").strip().lower(),
         openai_runtime=str(block.get("openai_runtime") or "").strip().lower(),
         nous_anthropic_wire=str(nous.get("anthropic_wire") or "").strip().lower())
try:
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli import runtime_provider as rp
except Exception as exc:
    done(kind="unavailable", error="Hermes is not importable from " + source + " (" + text(exc) + ")")
try:
    load_hermes_dotenv(hermes_home=home)
    cfg = rp._get_model_config()
    requested = rp.resolve_requested_provider()
except Exception as exc:
    done(kind="config", error="profile config unreadable (" + text(exc) + ")")
model = str(cfg.get("default") or "").strip()
if mode == "models":
    try:
        from hermes_cli import model_catalog
        block = model_catalog._get_provider_block(requested.split(":", 1)[0])
        ids = [mid for mid, _ in model_catalog._block_ids(block)]
    except Exception as exc:
        done(kind="catalog", error="Hermes catalog unavailable (" + text(exc) + ")",
             model=model, requested=requested)
    declared = []
    try:
        from hermes_cli.config import load_config
        entries = load_config().get("custom_providers") or []
        name = requested.split(":", 1)[1] if requested.startswith("custom:") else ""
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and name and str(entry.get("name") or "").strip().lower() == name:
                raw = entry.get("models") or []
                declared = [str(m.get("id") if isinstance(m, dict) else m) for m in
                            (raw if isinstance(raw, list) else list(raw))]
    except Exception:
        pass
    done(model=model, requested=requested, catalog=ids, declared=declared,
         catalog_known=block is not None)

# -- refusals that must happen before any credential is read ------------------------------------
def refuse(error):
    done(kind="unsupported", model=model, requested=requested, error=error)

if requested in ("", "auto"):
    refuse("the profile names no model.provider, so Hermes would auto-detect one")
if requested in unsupported:
    refuse("provider " + requested + " needs a token exchange, cloud signing or several models, "
           "which the inference proxy does not do")
if str(cfg.get("openai_runtime") or "").strip().lower() == "codex_app_server":
    refuse("model.openai_runtime is codex_app_server: the turn would run a codex subprocess "
           "with its own login, not a proxied API")
configured = str(cfg.get("api_mode") or "").strip().lower()
try:
    from hermes_cli.config_providers import _canonical_api_mode
    configured = _canonical_api_mode(configured).lower() if configured else ""
except Exception:
    pass
if configured in refused_modes:
    refuse("model.api_mode is " + configured + ", which the inference proxy cannot speak")
try:
    from hermes_cli.auth import PROVIDER_REGISTRY
    entry = PROVIDER_REGISTRY.get(requested)
    auth_type = str(getattr(entry, "auth_type", "api_key")) if entry is not None else "api_key"
    if auth_type != "api_key" and requested not in oauth_providers and requested not in anthropic_aliases:
        refuse("provider " + requested + " authenticates by " + auth_type +
               ", which the inference proxy cannot refresh")
except ImportError:
    pass

def resolve():
    runtime = rp.resolve_runtime_provider()
    key = runtime.get("api_key")
    return runtime, key() if callable(key) else key, callable(key)

try:
    runtime, key, minted = resolve()
except Exception as exc:
    done(kind="credential", model=model, requested=requested, error=text(exc))
api_mode = str(runtime.get("api_mode") or "")
provider = str(runtime.get("provider") or "")
if api_mode not in modes:
    refuse("provider " + (provider or requested) + " resolves to api_mode " + (api_mode or "?") +
           ", which the inference proxy cannot speak")

if mode == "refresh" and stale and digest(key) == stale:
    # The host saw this very token rejected (or expiring): make Hermes rotate it, under Hermes's
    # own auth.lock, then resolve again. A peer that refreshed first shows up as a new token.
    try:
        pool = runtime.get("credential_pool")
        if pool is not None and hasattr(pool, "try_refresh_matching"):
            pool.try_refresh_matching(api_key_hint=key)
        else:
            forced = {"openai-codex": "resolve_codex_runtime_credentials",
                      "xai-oauth": "resolve_xai_oauth_runtime_credentials",
                      "qwen-oauth": "resolve_qwen_runtime_credentials"}.get(provider)
            if forced and hasattr(rp, forced):
                getattr(rp, forced)(force_refresh=True)
            elif provider == "nous":
                from hermes_cli.auth import resolve_nous_runtime_credentials
                resolve_nous_runtime_credentials(force_refresh=True, stale_access_token=key)
        runtime, key, minted = resolve()
    except Exception as exc:
        done(kind="credential", model=model, requested=requested, error="refresh failed (" + text(exc) + ")")

base_url = str(runtime.get("base_url") or "")
key = key if isinstance(key, str) else ""
oauth = provider in oauth_providers or requested in oauth_providers
scheme, headers, identity, wire_model = "bearer", {}, "", model
if api_mode == "anthropic_messages":
    try:
        from agent import anthropic_adapter as aa
        from agent.anthropic_credentials import anthropic_route_is_oauth
        if anthropic_route_is_oauth(base_url, key, provider=provider):
            # A Claude subscription token: Bearer plus the Claude Code identity Hermes sends.
            oauth, identity = True, "claude_code"
            headers = {"anthropic-beta": ",".join(aa._common_betas_for_base_url(base_url) + list(aa._OAUTH_ONLY_BETAS)),
                       "user-agent": "claude-code/" + aa._get_claude_code_version() + " (external, cli)",
                       "x-app": "cli"}
        else:
            import re
            style = aa._auth_style(key, base_url, re.sub(r"/v1/?$", "", base_url.rstrip("/")))
            scheme = "x-api-key" if style == "api_key" else "bearer"
            headers = {"anthropic-beta": ",".join(aa._common_betas_for_base_url(base_url))}
            if style == "kimi":
                headers.update(aa._attribution_headers())
        if not aa._is_nous_portal_endpoint(base_url):
            wire_model = aa.normalize_model_name(model)
    except Exception as exc:
        done(kind="unsupported", model=model, requested=requested,
             error="cannot derive the Anthropic wire headers from this Hermes (" + text(exc) + ")")
else:
    try:
        from agent.agent_init import _host_default_headers_factory
        factory = _host_default_headers_factory(base_url)
        headers = dict(factory(key, base_url)) if factory else {}
    except Exception:
        headers = {}
    if api_mode == "codex_responses" and not headers and "chatgpt.com" in base_url:
        try:
            from agent.codex_headers import codex_cloudflare_headers
            headers = codex_cloudflare_headers(key, base_url=base_url)
        except Exception:
            headers = {}
    if api_mode == "codex_responses":
        try:
            from agent.model_metadata import strip_codex_context_variant_suffix
            wire_model = strip_codex_context_variant_suffix(model) or model
        except Exception:
            pass
done(model=model, requested=requested, provider=provider, api_mode=api_mode, base_url=base_url,
     key=key, auth="oauth" if oauth else "api_key", scheme=scheme,
     headers={str(k): str(v) for k, v in (headers or {}).items() if v is not None},
     expires_at=expiry(runtime, key) if oauth else None, wire_model=wire_model,
     identity=identity, refreshable=bool(oauth or minted))
'''


def hermes_interpreter(settings: dict | None) -> tuple[str, str]:
    """(python, source) for running Hermes: the runtime's venv and checkout, else this process."""
    if settings and settings.get("venv") and settings.get("source"):
        return str(Path(settings["venv"]) / "bin" / "python"), str(settings["source"])
    try:
        import hermes_cli  # noqa: F401 — the plugin normally runs inside Hermes
    except ImportError:
        raise SeatModelError("Hermes is not importable here and no runtime file names its venv "
                             "and source; write $HERMES_HOME/review-loop-runtime.json") from None
    return sys.executable, str(Path(hermes_cli.__file__).resolve().parents[1])


def _redact(text: str, key: str = "") -> str:
    text = str(text)
    if key and len(key) >= 4:
        text = text.replace(key, "[REDACTED]")
    return _SECRETISH.sub("[REDACTED]", text)[:400]


_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def lock_dir() -> Path:
    """Where the cross-process profile locks live: review-loop's host state, not a profile."""
    return config.home() / "state" / "review-loop-seat-locks"


@contextlib.contextmanager
def profile_lock(profile: str):
    """Serialize every credential resolution of one profile, across threads and processes.

    Two seats (or two turns) sharing a profile must not refresh its OAuth token in parallel: the
    second waits, then resolves the token the first one refreshed. Hermes's own ``auth.lock``
    still guards ``auth.json`` inside the resolution; this lock keeps review-loop from even
    starting a second resolution meanwhile. The lock file lives in review-loop's state
    directory, never in the profile.
    """
    home = str(config.profile_dir(profile).expanduser().resolve())
    with _PROFILE_LOCKS_GUARD:
        local = _PROFILE_LOCKS.setdefault(home, threading.Lock())
    with local:
        try:
            # The host (gate enqueue) creates it; a worker never recreates host state, and
            # without it falls back to the in-process lock below.
            directory = hostdirs.ensure(lock_dir(), mode=0o700)
            handle = open(directory / (hashlib.sha256(home.encode()).hexdigest()[:24] + ".lock"), "a")
        except OSError:
            handle = None                       # read-only state: the thread lock still holds
        try:
            if handle is not None:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    pass
            yield
        finally:
            if handle is not None:
                handle.close()                  # closing releases the flock


def run_resolver(profile: str, mode: str, settings: dict | None,
                 timeout: int = RESOLVE_TIMEOUT, stale: str = "") -> dict:
    """Run Hermes as ``profile`` in its own process and return its JSON answer.

    The child's environment is built from scratch: HOME, PATH, and HERMES_HOME set to the profile
    home. Nothing from this process's environment — which, inside Hermes, holds the launch
    profile's provider keys — reaches the seat's resolution. ``resolve`` and ``refresh`` (which
    may rotate an OAuth token) run under ``profile_lock``; ``stale`` is the SHA-256 of a token
    the upstream rejected, so Hermes rotates that token rather than hand it back.
    """
    home = config.profile_dir(profile)
    python, source = hermes_interpreter(settings)
    try:
        import pwd
        user_home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        user_home = os.environ.get("HOME", "/")
    if config.test_guard_active():
        user_home = os.environ.get("HOME", "/")   # a guarded test never hands Hermes the real HOME
    env = {"PATH": "/usr/bin:/bin", "HOME": user_home,
           "HERMES_HOME": str(home), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           # A resolution, not a Hermes launch: a source checkout with a pending dependency sync
           # otherwise has Hermes's bootstrap (``venv_sync.prepare_launch``) re-exec this child
           # into its package manager's interpreter, keeping our ``-E`` -- which hides the
           # environment that interpreter needs, so Hermes is unimportable and the answer lands
           # on the swapped stdout. Hermes's own opt-out for launch preparation.
           "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    from .inference_proxy import SUPPORTED_MODES
    policy = json.dumps({"unsupported": sorted(UNSUPPORTED_PROVIDERS), "oauth": OAUTH_PROVIDERS,
                         "modes": sorted(SUPPORTED_MODES), "refused_modes": sorted(REFUSED_API_MODES),
                         "anthropic": sorted(ANTHROPIC_ALIASES), "stale": stale})
    locked = profile_lock(profile) if mode in ("resolve", "refresh") else contextlib.nullcontext()
    try:
        with locked:
            process = subprocess.run([python, "-E", "-s", "-B", "-c", util.leak_guard_code(_RESOLVER),
                                      source, mode, policy],
                                     env=util.leak_guard_env(env, pythonpath=False), cwd=str(home), stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise SeatModelError(f"profile {profile}: Hermes did not resolve within {timeout}s") from None
    except OSError as exc:
        raise SeatModelError(f"profile {profile}: cannot run Hermes at {python} "
                             f"({type(exc).__name__}); check venv/source in the runtime file") from None
    try:
        answer = json.loads(process.stdout[:65536].decode("utf-8"))
        if not isinstance(answer, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        # Name what the child said last (redacted): "no answer" alone sends the operator to the
        # runtime file when the reason -- an import error, a relaunch -- is right there.
        said = [line for line in process.stderr[-4096:].decode("utf-8", "replace").splitlines()
                if line.strip()]
        why = f": {_redact(said[-1])[:200]}" if said else ""
        raise SeatModelError(f"profile {profile}: Hermes gave no answer "
                             f"(rc={process.returncode}){why}; check venv/source in the runtime "
                             "file") from None
    return answer


def seats_for(loop: dict) -> list[str]:
    """The seats this loop can run isolated turns for (the adjudicator only with its route)."""
    return ["reviewer", "fixer"] + (
        ["adjudicator"] if str((loop.get("adjudicator") or {}).get("route") or "") else [])


# Credential files Hermes may read for an OAuth/subscription seat outside the profile directory
# (Claude Code's login, the Codex CLI's, Qwen's) — in the user's real home, never in the sandbox.
USER_CREDENTIAL_FILES = (".claude/.credentials.json", ".codex/auth.json", ".qwen/oauth_creds.json")
PROFILE_SECRET_FILES = (".env", "auth.json", "auth.lock", "config.yaml", ".anthropic_oauth.json")


def secret_paths(loop: dict, settings: dict | None) -> list[str]:
    """Host files holding a seat's provider credential — none may be readable in the sandbox."""
    paths: list[str] = []
    for block in [legacy_override(settings or {}), *((settings or {}).get("seats") or {}).values()]:
        if block:
            paths.append(str(Path(block["key_file"]).expanduser()))
    for seat in seats_for(loop):
        home = config.profile_dir(config.seat_profile(loop, seat))
        paths += [str(home / name) for name in PROFILE_SECRET_FILES]
    try:
        import pwd
        user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        user_home = Path.home()
    paths += [str(user_home / name) for name in USER_CREDENTIAL_FILES]
    return list(dict.fromkeys(paths))


def profile_problem(profile: str, seat: str) -> str:
    """Why this profile name cannot be used at all, or ``""``."""
    if not profile:
        return f"no Hermes profile is configured for the {seat} seat"
    if not config.profile_exists(profile):
        return (f"profile {profile} does not exist at {config.profile_dir(profile)} "
                "(or has no config.yaml)")
    return ""


def _headers(raw: object) -> tuple:
    if not isinstance(raw, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in raw.items()))


def _expiry(raw: object) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def resolve_profile(profile: str, seat: str, settings: dict | None, *,
                    stale: str = "") -> SeatInference:
    """The seat's model from its profile, via Hermes; ``SeatModelError`` with a fix otherwise.

    ``stale`` (a SHA-256 of a rejected access token) asks Hermes to rotate that token first —
    the host's refresh path; the caller never sees a refresh token either way.
    """
    problem = profile_problem(profile, seat)
    if problem:
        raise SeatModelError(problem)
    answer = run_resolver(profile, "refresh" if stale else "resolve", settings, stale=stale)
    key = answer.get("key") if isinstance(answer.get("key"), str) else ""
    where = f"profile {profile}"
    requested = str(answer.get("requested") or "")
    fixes = {"unsupported": "set this profile's model.provider to one whose Hermes runtime is "
                            "chat_completions, codex_responses or anthropic_messages (an API-key "
                            "provider, openai-codex, xai-oauth, qwen-oauth, nous, minimax-oauth, "
                            "anthropic), or add a seats." + seat + " override to the runtime file",
             "credential": f"give profile {profile} its provider login or key (`hermes -p {profile} "
                           "auth`, or its .env), or add a seats override",
             "config": f"repair {config.profile_dir(profile) / 'config.yaml'}",
             "interpreter": "name a venv with Hermes's own dependencies in the runtime file "
                            "($HERMES_HOME/review-loop-runtime.json): the interpreter the host "
                            "picked cannot read YAML, so no profile's model can be resolved",
             "unavailable": "point source/venv in the runtime file at the Hermes install"}
    if answer.get("error"):
        kind = str(answer.get("kind") or "")
        raise SeatModelError(f"{where}{' (' + requested + ')' if requested else ''}: "
                             f"{_redact(answer['error'], key)} — {fixes.get(kind, 'see above')}")
    from .inference_proxy import SUPPORTED_MODES
    model = str(answer.get("model") or "").strip()
    provider = str(answer.get("provider") or requested)
    api_mode = str(answer.get("api_mode") or "")
    if api_mode not in SUPPORTED_MODES:
        raise SeatModelError(f"{where} ({requested}): provider speaks {api_mode or 'an unknown API'}, "
                             "which the inference proxy cannot forward — " + fixes["unsupported"])
    if not model:
        raise SeatModelError(f"{where}: no model.default set — `hermes -p {profile} model`")
    if not key or "\n" in key or "\r" in key:
        raise SeatModelError(f"{where} ({requested}): resolved without a usable credential — "
                             f"{fixes['credential']}")
    upstream = upstream_for(str(answer.get("base_url") or ""), where, api_mode)
    auth = "oauth" if answer.get("auth") == "oauth" else "api_key"
    scheme = "x-api-key" if answer.get("scheme") == "x-api-key" else "bearer"
    try:
        from .inference_proxy import Credential
        Credential(key, scheme, _headers(answer.get("headers")))
    except ValueError:
        raise SeatModelError(f"{where} ({requested}): Hermes gave headers the proxy will not "
                             "send (credential-like or malformed)") from None
    wire = str(answer.get("wire_model") or "").strip()
    return SeatInference(seat, profile, "profile", requested or provider, model, upstream, key,
                         api_mode=api_mode, auth=auth, scheme=scheme,
                         headers=_headers(answer.get("headers")),
                         expires_at=_expiry(answer.get("expires_at")),
                         wire_model=wire if wire and wire != model else "",
                         client_identity="claude_code" if answer.get("identity") == "claude_code" else "",
                         refreshable=bool(answer.get("refreshable")), settings=settings)


def resolve_seat(loop: dict, seat: str, settings: dict, *, resolver=None) -> SeatInference:
    """Apply the precedence (override > profile > legacy) for one seat, or raise with the reason."""
    if seat not in SEATS:
        raise SeatModelError(f"unknown seat {seat!r}")
    resolver = resolver or resolve_profile
    profile = config.seat_profile(loop, seat)
    override = seat_override(settings, seat)
    if override is not None:
        return _from_override(override, seat, profile, "override", f"runtime seats.{seat}")
    try:
        return resolver(profile, seat, settings)
    except SeatModelError as exc:
        legacy = legacy_override(settings)
        if legacy is None:
            raise
        return _from_override(legacy, seat, profile, "legacy", "runtime model/upstream/key_file",
                              warning=f"{exc}; using the legacy runtime model instead")


def expected_wire(requested: str, api_mode: str = "") -> tuple[str, str]:
    """``(api_mode, auth label)`` a provider is expected to resolve to — no credential lookup."""
    if requested in OAUTH_PROVIDERS:
        return OAUTH_PROVIDERS[requested], "OAuth (host-refreshed)"
    if requested in ANTHROPIC_ALIASES:
        return "anthropic_messages", "API key, or Claude subscription OAuth (host-refreshed)"
    return api_mode or "chat_completions", "API key"


def describe_seat(loop: dict, seat: str, settings: dict | None) -> tuple[str, str, str]:
    """Read-only (no credential lookup) — ``(status, detail, fix)`` with status ok|warn|fail."""
    return describe_seat_wire(loop, seat, settings)[:3]


def describe_seat_wire(loop: dict, seat: str,
                       settings: dict | None) -> tuple[str, str, str, dict | None]:
    """``describe_seat`` plus the wire the seat is expected to use — ``{"provider", "api_mode",
    "base_url"}`` when it is decided (a profile's provider, a runtime override, or the legacy
    fallback), else ``None``: a seat whose provider could not be read has no wire to reason
    about."""
    profile = config.seat_profile(loop, seat)
    override = seat_override(settings or {}, seat)
    if override is not None:
        return ("ok", f"runtime override seats.{seat}: {override['model']} via "
                      f"{urlsplit(override['upstream']).hostname} [chat_completions, API key] "
                      f"(profile {profile or '-'} unused)", "",
                {"provider": "override", "api_mode": "chat_completions",
                 "base_url": override["upstream"]})
    legacy = legacy_override(settings or {})
    problem = profile_problem(profile, seat)
    reason = problem
    if not problem:
        try:
            answer = run_resolver(profile, "describe", settings)
        except SeatModelError as exc:
            return ("warn", f"profile {profile}: {exc}", "run `hermes review-loop selftest`", None)
        requested = str(answer.get("requested") or "auto")
        model = str(answer.get("model") or "")
        hermes = answer.get("hermes") if isinstance(answer.get("hermes"), dict) else None
        # Hermes could not be asked: say so. "missing" means the turn itself cannot run (it
        # resolves through the same import); "drift" means this Hermes lacks what doctor asks, so
        # the verdicts below rest on doctor's own table and are at most warnings.
        unavailable = str((hermes or {}).get("unavailable") or "")
        not_asked = _redact(str((hermes or {}).get("detail") or "")) if unavailable else ""
        if unavailable or hermes is None:
            not_asked = not_asked or "the describe step did not report Hermes's answers"
            hermes = None
        entry = (hermes or {}).get("entry") if isinstance((hermes or {}).get("entry"), dict) else None
        # Hermes's own reading of model.api_mode when it can be asked (every alias included);
        # a named custom provider ignores it — its entry decides.
        configured = ("" if entry else (hermes or {}).get("configured", "")
                      if hermes is not None else _canonical_mode(str(answer.get("api_mode") or "")))
        entry_mode = _canonical_mode(str((entry or {}).get("api_mode") or ""))
        if entry and entry.get("model"):
            model = str(entry["model"])
        if answer.get("error"):
            reason = f"profile {profile}: {_redact(answer['error'])}"
            if str(answer.get("kind") or "") == "interpreter":
                return ("fail", f"{reason}; the {seat} turn will be held",
                        "name a venv with Hermes's own dependencies in "
                        f"{config.home() / 'review-loop-runtime.json'} — the interpreter the host "
                        "picked cannot read YAML at all (docs/configuration.md)", None)
        elif requested in ("", "auto"):
            reason = f"profile {profile} names no model.provider"
        elif unavailable == "missing":
            reason = (f"profile {profile}: {not_asked} — the turn resolves its model through that "
                      "same import")
            fix_missing = ("point source/venv in "
                           f"{config.home() / 'review-loop-runtime.json'} at the Hermes install")
            if legacy is None:
                return ("fail", f"{reason}; the {seat} turn will be held", fix_missing, None)
        elif hermes is not None and hermes.get("error"):
            reason = f"profile {profile}: {_redact(str(hermes['error']))}"
        elif hermes is not None and hermes.get("url_error") and not entry:
            reason = (f"profile {profile}: Hermes cannot read model.base_url "
                      f"({_redact(str(hermes['url_error']))})")
        elif requested in UNSUPPORTED_PROVIDERS:
            reason = (f"profile {profile} uses {requested}, which the inference proxy cannot "
                      "speak (token exchange, cloud signing or several models)")
        elif answer.get("openai_runtime") == "codex_app_server":
            reason = (f"profile {profile} runs the codex_app_server runtime, which the inference "
                      "proxy cannot carry")
        elif entry_mode in REFUSED_API_MODES:
            reason = (f"profile {profile}'s {entry.get('where')} sets api_mode {entry_mode}, "
                      "which the proxy cannot speak")
        elif configured in REFUSED_API_MODES:
            reason = f"profile {profile} sets api_mode {configured}, which the proxy cannot speak"
        elif not model:
            reason = f"profile {profile} has no model.default"
        else:
            base = answer.get("base_url") or ""
            mode, label = expected_wire(requested, configured)
            wire = {"provider": requested, "api_mode": mode, "base_url": str(base),
                    "model": model, "configured": configured, "entry": entry, "hermes": hermes,
                    "entry_hint": bool(answer.get("entry_hint")), "not_asked": not_asked,
                    "facts": {"nous_anthropic_wire": str(answer.get("nous_anthropic_wire") or "")}}
            explain: dict = {}
            extras_for(requested, mode, str(base), model=model, configured=configured,
                       facts=wire["facts"], entry=entry, hermes=hermes,
                       entry_hint=wire["entry_hint"], explain=explain)
            # The wire Hermes will use, when decided — not the profile's own spelling.
            wire["api_mode"] = mode = explain.get("wire") or mode
            if entry:
                label = "API key"
            shown = (entry or {}).get("url") or base
            detail = (f"profile {profile}: {requested} / {model}"
                      + (f" via {urlsplit(shown).hostname}" if shown else "")
                      + f" [{mode}, {label}]")
            if not_asked:
                return ("warn", f"{detail} by doctor's own table, NOT by Hermes: {not_asked}",
                        "point source/venv in the runtime file at the Hermes install the turn runs "
                        "(then re-run doctor)", wire)
            return ("ok", detail + " (credential checked by selftest)", "", wire)
    fix = (f"set a supported provider in profile {profile or '<name>'} (see `docs/configuration.md`), "
           f"or add seats.{seat} to the runtime file")
    if legacy is not None:
        return ("warn", f"{reason}; falls back to the LEGACY runtime model {legacy['model']} "
                        "(every such seat shares it)", fix,
                {"provider": "legacy", "api_mode": "chat_completions",
                 "base_url": str(legacy.get("upstream") or "")})
    return ("fail", f"{reason}; the {seat} turn will be held", fix, None)
