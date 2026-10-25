"""Create a loop's GitHub Apps from manifests (#1163 M4): one browser click per App, no form to
fill in, no key or secret ever copied by hand.

GitHub's App-manifest flow: the operator's browser POSTs a manifest (name, permissions, events,
webhook URL, the URL to come back to) to ``github.com/settings/apps/new``; after the operator's
"Create GitHub App" click, GitHub sends the browser back with a one-time ``code`` (valid one hour);
the host exchanges it (``POST /app-manifests/{code}/conversions``, no credential) for the App's id,
slug, private key and webhook secret.

The browser comes back to a server on ``127.0.0.1`` that this module runs for one App at a time:
it answers only requests addressed to that exact host and port (a DNS-rebinding page cannot reach
it), only ``/`` (the page that posts the manifest) and ``/callback``, and accepts a code only with
the random ``state`` it issued. A browser on another machine cannot reach it, so the code can be
pasted instead (``--no-browser`` writes the one-click page to a file). Private keys are written ``0600``, never over an existing file,
never printed. A session file without secrets records the Apps already made, so a rerun resumes.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

GITHUB = "https://github.com"
ROLES = ("reader", "reviewer", "fixer", "adjudicator")
WAIT_S = 600                    # how long one App's browser round trip may take
SESSION = "apps-wizard.json"

# Each identity's repository permissions and events, as the live trial proved them (#1163,
# docs/accounts.md). Metadata read is always granted. Only the reader's webhook is active: it is
# the App whose webhook delivers the loop's events (ingress app-webhook), and it is the hook admin.
PERMISSIONS = {
    "reader": {"contents": "read", "pull_requests": "read", "checks": "read", "statuses": "read",
               "issues": "read", "repository_hooks": "write", "administration": "write"},
    "reviewer": {"contents": "read", "pull_requests": "write", "issues": "write",
                 "checks": "write"},
    "fixer": {"contents": "write", "pull_requests": "write", "issues": "write"},
    "adjudicator": {"pull_requests": "write"},
}
EVENTS = {"reader": ["pull_request", "pull_request_review", "issues"]}


class WizardError(Exception):
    """The wizard stopped; the message says why and how to resume."""


def slug_of(name: str) -> str:
    """The slug GitHub gives an App named ``name`` (lowercase, runs of anything else to one ``-``)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def app_name(prefix: str, role: str) -> str:
    name = f"{prefix}-{role}"
    if len(name) > 34:                     # GitHub's App-name limit
        raise WizardError(f"{name!r} is longer than GitHub allows for an App name (34): use a "
                          "shorter --prefix")
    return name


def manifest(prefix: str, role: str, host: str, redirect: str, homepage: str) -> dict:
    """The manifest for one role's App."""
    name = app_name(prefix, role)
    body = {"name": name, "url": homepage, "redirect_url": redirect, "public": False,
            "default_permissions": {"metadata": "read", **PERMISSIONS[role]},
            "default_events": EVENTS.get(role, [])}
    if role == "reader":
        # The route the reader's webhook delivers to (config.app_route: dk-app-<slug>). Its
        # secret is GitHub's here; `hermes dk app-webhook --set` replaces it with the route's.
        body["hook_attributes"] = {"url": f"{host.rstrip('/')}/webhooks/dk-app-{slug_of(name)}",
                                   "active": True}
    else:
        body["hook_attributes"] = {"url": f"{host.rstrip('/')}/webhooks/none", "active": False}
    return body


def new_app_url(owner: str, org: bool, state: str) -> str:
    base = (f"{GITHUB}/organizations/{urllib.parse.quote(owner)}/settings/apps/new" if org
            else f"{GITHUB}/settings/apps/new")
    return f"{base}?state={urllib.parse.quote(state)}"


def form_page(action: str, body: dict) -> bytes:
    """The page that posts the manifest to GitHub (one click, or by itself with JavaScript)."""
    value = html.escape(json.dumps(body), quote=True)
    return (f"""<!doctype html><meta charset="utf-8"><title>Create {html.escape(body['name'])}</title>
<form id="f" method="post" action="{html.escape(action, quote=True)}">
<input type="hidden" name="manifest" value="{value}">
<p>Create the GitHub App <b>{html.escape(body['name'])}</b> for Diaktoros.</p>
<button type="submit">Continue to GitHub</button></form>
<script>document.getElementById("f").submit()</script>""").encode()


def convert(code: str, post=None) -> dict:
    """Exchange a manifest ``code`` for the App: ``id``, ``slug``, ``pem``, ``webhook_secret``…"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", code or ""):
        raise WizardError("that is not a manifest code (GitHub's redirect carries ?code=…)")
    if post is None:
        import urllib.request
        from .config.host_guards import guard_network

        def post(url):
            req = urllib.request.Request(url, data=b"", method="POST", headers={
                "Accept": "application/vnd.github+json", "User-Agent": "diaktoros"})
            guard_network(req.full_url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "null")
    try:
        data = post(f"https://api.github.com/app-manifests/{code}/conversions")
    except Exception as exc:
        raise WizardError(f"GitHub did not convert the code ({type(exc).__name__}: {exc}); a code "
                          "is single-use and expires after an hour: rerun to make it again") from None
    if not (isinstance(data, dict) and type(data.get("id")) is int and data.get("slug")
            and str(data.get("pem") or "").startswith("-----BEGIN")):
        raise WizardError("GitHub's answer did not carry the App's id, slug and key")
    return data


def write_key(key_dir: Path, slug: str, pem: str) -> Path:
    """The App's private key, 0600, never over an existing file (it is the App's only copy)."""
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)
    path = key_dir / f"{slug}.pem"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem.encode())
    finally:
        os.close(fd)
    return path


def write_page(key_dir: Path, name: str, page: bytes) -> Path:
    """The --no-browser manifest page (it carries the state, so 0600 like the keys)."""
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = key_dir / f"{name}-manifest.html"
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(page)
    return path


def load_session(key_dir: Path) -> dict:
    try:
        data = json.loads((key_dir / SESSION).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_session(key_dir: Path, session: dict) -> None:
    """What was made, without a key or secret: a rerun skips these roles."""
    key_dir.mkdir(parents=True, exist_ok=True)
    tmp = key_dir / (SESSION + ".tmp")
    tmp.write_text(json.dumps(session, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, key_dir / SESSION)


class _Round:
    """One App's browser round trip on 127.0.0.1: the manifest page, then the callback."""

    def __init__(self, action: str, body_for, state: str):
        self.action, self.body_for, self.state = action, body_for, state
        self.code: str | None = None
        self.error = ""
        self.done = threading.Event()
        round_ = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):        # nothing about the request reaches stdout
                pass

            def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):                     # noqa: N802 - http.server's name
                host = self.headers.get("Host", "")
                if host != f"127.0.0.1:{self.server.server_port}":
                    return self._send(403, b"wrong host")        # a rebinding page, or a proxy
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/":
                    return self._send(200, form_page(round_.action, round_.body_for(
                        f"http://127.0.0.1:{self.server.server_port}/callback")))
                if url.path == "/callback":
                    query = urllib.parse.parse_qs(url.query)
                    state = (query.get("state") or [""])[0]
                    if not hmac.compare_digest(state.encode(), round_.state.encode()):
                        return self._send(403, b"state does not match: this callback is not ours")
                    if round_.done.is_set():
                        return self._send(409, b"already received")
                    round_.code = (query.get("code") or [""])[0]
                    round_.done.set()
                    return self._send(200, b"<p>Done: return to the terminal.</p>")
                return self._send(404, b"not found")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port

    def run(self, timeout: float) -> str | None:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            self.done.wait(timeout)
        finally:
            self.server.shutdown()
            self.server.server_close()
        return self.code


def code_from(text: str, state: str) -> str:
    """A pasted redirect URL (state checked) or a bare code."""
    text = text.strip()
    if "?" in text or text.startswith("http"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
        if not hmac.compare_digest(((query.get("state") or [""])[0]).encode(), state.encode()):
            raise WizardError("that URL's state is not this App's: paste the URL GitHub sent you "
                              "back to for this App")
        return (query.get("code") or [""])[0]
    return text


def create(*, owner: str, org: bool, prefix: str, roles, host: str, key_dir: Path,
           browser: bool = True, emit=print, ask=input, open_url=None, post=None,
           wait_s: float = WAIT_S) -> dict:
    """Make each role's App (skipping ones the session already has); role -> made App record."""
    for role in roles:                     # every name checked before any browser opens
        if role not in ROLES:
            raise WizardError(f"unknown role {role!r}: choose from {', '.join(ROLES)}")
        app_name(prefix, role)
    session = load_session(key_dir)
    made = dict(session.get("apps") or {})
    homepage = "https://github.com/coe0718/diaktoros"
    for role in roles:
        if role in made:
            emit(f"  {role}: {made[role]['slug']} (made earlier, key {made[role]['key_file']})")
            continue
        state = secrets.token_urlsafe(32)
        action = new_app_url(owner, org, state)
        if browser:
            round_ = _Round(action, lambda redirect, r=role: manifest(prefix, r, host, redirect,
                                                                     homepage), state)
            start = f"http://127.0.0.1:{round_.port}/"
            emit(f"  {role}: opening {start} — click 'Create GitHub App' on GitHub")
            if open_url is None:
                import webbrowser
                open_url = webbrowser.open
            open_url(start)
            code = round_.run(wait_s)
            if not code:
                raise WizardError(f"no answer from GitHub for the {role} App within "
                                  f"{int(wait_s)}s: rerun to try again (made Apps are kept)")
        else:
            # GitHub takes a manifest only as a form POST, so the one-click page goes to a file
            # any browser can open; GitHub then sends it to the homepage with ?code=&state=.
            page = write_page(key_dir, app_name(prefix, role),
                              form_page(action, manifest(prefix, role, host, homepage, homepage)))
            emit(f"  {role}: open {page} in a browser (copy it to that machine if needed), click "
                 f"'Create GitHub App', then paste the address GitHub sends you to")
            code = code_from(ask(f"  {role} redirect URL or code: "), state)
            page.unlink(missing_ok=True)
        app = convert(code, post=post)
        key = write_key(key_dir, str(app["slug"]), str(app["pem"]))
        made[role] = {"slug": app["slug"], "id": app["id"], "login": f"{app['slug']}[bot]",
                      "key_file": str(key), "made_at": int(time.time())}
        session.update(apps=made, owner=owner, org=org, prefix=prefix, host=host)
        save_session(key_dir, session)
        emit(f"  {role}: made {app['slug']} (App {app['id']}), key {key} (mode 600)")
    return made


def next_steps(made: dict, owner: str, org: bool, loop_id: str | None) -> list[str]:
    """What the operator runs next: install each App, then map the identities."""
    steps = [f"install each App on the repository (only that one): " +
             " · ".join(f"{GITHUB}/apps/{rec['slug']}/installations/new" for rec in made.values())]
    if loop_id:
        for role, rec in made.items():
            seat = "read" if role == "reader" else role
            steps.append(f"hermes dk identity --loop {loop_id} --seat {seat} --app "
                         f"\"{rec['login']}={rec['id']}:{rec['key_file']}\"")
        if "reader" in made:
            steps += [f"hermes dk set --loop {loop_id} --ingress app-webhook --ingress-app "
                      f"\"{made['reader']['login']}\"",
                      f"hermes dk app-webhook --loop {loop_id} --set"]
        if "reviewer" in made:
            steps.append(f"hermes dk protect --loop {loop_id} --admin-token "
                         f"\"{made.get('reader', made['reviewer'])['login']}\"")
    else:
        apps = " ".join(f"--app \"{rec['login']}={rec['id']}:{rec['key_file']}\""
                        for rec in made.values())
        steps.append(f"hermes dk setup --repo OWNER/REPO {apps} (or `init`, with --ingress "
                     "app-webhook --ingress-app <reader login>)")
    return steps
