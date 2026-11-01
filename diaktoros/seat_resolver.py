"""The program ``seat_model.run_resolver`` runs in Hermes' own python (#32), split from
``seat_model.py`` (#1349).

``RESOLVER`` is source, not code of this plugin: it runs with ``-E -s -B -c`` as the seat's profile,
cannot import the plugin, and answers one JSON object on its stdout. ``seat_model`` reads it as
``seat_resolver.RESOLVER`` when it builds the command line, so a test that patches it here reaches
the call.
"""

RESOLVER = r'''
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
                           "$HERMES_HOME/diaktoros-runtime.json (docs/configuration.md)") from None



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
    agent = cfg.get("agent") if isinstance(cfg, dict) else None
    effort = agent.get("reasoning_effort") if isinstance(agent, dict) else None
    effort = ("" if effort in (None, "", True) else "none" if effort is False
              else str(effort).strip().lower()[:32])
    done(hermes=hermes_facts(str(block.get("provider") or ""), block), effort=effort,
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


def reasoning_effort():
    """The profile's agent.reasoning_effort as Hermes reads it (#874), or "" when unset."""
    try:
        from hermes_cli.config import load_config
        agent = load_config().get("agent")
        value = agent.get("reasoning_effort") if isinstance(agent, dict) else None
    except Exception:
        return ""
    if value is None or value == "" or value is True:
        return ""
    if value is False:
        return "none"
    return str(value).strip().lower()[:32]


effort = reasoning_effort()
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
    if auth_type != "api_key" and requested not in oauth_providers and requested not in anthropic_aliases and requested != "claude-subscription-directsdk-experimental":
        refuse("provider " + requested + " authenticates by " + auth_type +
               ", which the inference proxy cannot refresh")
except ImportError:
    pass

def resolve():
    if requested == "claude-subscription-directsdk-experimental":
        os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.path.join(os.environ["HOME"], ".local/bin")
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

if requested == "claude-subscription-directsdk-experimental":
    if (provider != requested or api_mode != "chat_completions" or
            runtime.get("base_url") != "process://" + requested or runtime.get("args")):
        refuse("DirectSDK runtime has an unsupported process contract")
    done(model=model, requested=requested, provider=provider, api_mode=api_mode,
         base_url="process://" + requested, key="host-process", auth="external_process",
         effort=effort)

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
     identity=identity, refreshable=bool(oauth or minted), effort=effort)
'''
