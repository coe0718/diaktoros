"""A loop whose events arrive through a GitHub App's own webhook (#1239).

One App's webhook URL serves every repository it is installed on, so its route is shared by every
loop it delivers for (``config.app_route``), and its script (``scripts/gate_app.py``) picks the
gate from the payload's shape: the Hermes gateway runs a route script with the payload only, not
the event name.

GitHub cannot pause an App's webhook for one repository, so pausing such a loop is a flag in the
loop's own state that the dispatcher reads before any gate runs; other loops on the same App are
unaffected.
"""
from __future__ import annotations

import json

FILE = "app-ingress.json"

# The gate each event shape belongs to. A review payload also carries ``pull_request``, so it is
# tested first; comment events (``issue_comment``, ``pull_request_review_comment``) carry a
# ``comment`` and are not loop events.
GATES = ("gate_fixer", "gate_reviewer", "gate_triage")


def gate_for(payload: dict) -> tuple[str, str]:
    """``(gate script name, "")``, or ``("", why it is not a loop event)``."""
    if not isinstance(payload, dict):
        return "", "payload is not an object"
    if "zen" in payload:
        return "", "ping"
    if "comment" in payload:
        return "", "a comment event, not one the loop acts on"
    if not isinstance(payload.get("repository"), dict):
        return "", "no repository (an installation event)"
    if isinstance(payload.get("review"), dict):
        return "gate_fixer", ""
    if isinstance(payload.get("pull_request"), dict):
        return "gate_reviewer", ""
    if isinstance(payload.get("issue"), dict):
        return "gate_triage", ""
    return "", "an event the loop does not act on"


def paused(st) -> bool:
    """Whether this loop's App deliveries are dropped (``arm --pause``). Lock-free and read-only;
    an unreadable flag is armed, the same answer as a missing one."""
    try:
        path = st.dir / FILE
        if not path.exists():
            return False
        data = json.loads(path.read_text())
    except Exception:
        return False
    return isinstance(data, dict) and data.get("paused") is True


def set_paused(st, value: bool) -> None:
    with st.locked():
        memory = st._load(st.dir / FILE, {})
        memory = memory if isinstance(memory, dict) else {}
        memory["paused"] = bool(value)
        st._save_strict(st.dir / FILE, memory)


# The events an App's webhook must subscribe to for a loop: triage's only when it is on.
def events_needed(loop: dict) -> tuple[str, ...]:
    from . import config
    return ("pull_request", "pull_request_review") + (("issues",) if config.triage_enabled(loop)
                                                      else ())


def webhook_state(loop: dict) -> dict:
    """What the ingress App's webhook says against what this loop needs, read with the App's own
    JWT: ``{"url", "want_url", "content_type", "events", "missing_events", "error"}``."""
    from . import config, github_app, routes
    app = str(loop.get("ingress_app") or "")
    name = config.app_route(loop)
    target = routes.target(name, loop.get("host")) if name else None
    out = {"url": "", "want_url": target[0] if target else "", "content_type": "", "events": [],
           "missing_events": [], "error": ""}
    cfg = github_app._as_app(loop, app, "/app/hook/config")
    if cfg.error or not isinstance(cfg.data, dict):
        out["error"] = f"the App's webhook config could not be read: {cfg.error or 'no answer'}"
        return out
    out.update(url=str(cfg.data.get("url") or ""), content_type=str(cfg.data.get("content_type") or ""))
    meta = github_app._as_app(loop, app, "/app")
    if meta.error or not isinstance(meta.data, dict):
        out["error"] = f"the App's events could not be read: {meta.error or 'no answer'}"
        return out
    out["events"] = [str(e) for e in meta.data.get("events") or []]
    out["missing_events"] = [e for e in events_needed(loop) if e not in out["events"]]
    return out


def set_webhook(loop: dict) -> tuple[bool, str]:
    """Point the ingress App's webhook at its route, with the route's secret (never printed)."""
    from . import config, github_app, routes
    name = config.app_route(loop)
    target = routes.target(name, loop.get("host")) if name else None
    if not target:
        return False, f"route {name or '(none)'} is not installed with a secret: run `apply` first"
    url, secret = target
    done = github_app._as_app(loop, str(loop.get("ingress_app") or ""), "/app/hook/config",
                              "PATCH", {"url": url, "content_type": "json",
                                        "secret": secret.decode() if isinstance(secret, bytes) else secret,
                                        "insecure_ssl": "0"})
    if done.error or not isinstance(done.data, dict):
        return False, f"GitHub refused the webhook config: {done.error or 'no answer'}"
    return done.data.get("url") == url, f"{loop.get('ingress_app')}'s webhook → {url} (secret set)"
