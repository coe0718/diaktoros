"""GitHub App identities (#1163 M1b): a seat or the reader as an App's bot instead of a person.

A loop names an App identity in ``apps`` instead of ``tokens``::

    "apps": {"acme-reviewer[bot]": {"app_id": 5416187, "key_file": "/abs/acme-reviewer.pem"}}

The key stays on the host. For each GitHub call the host signs a short App JWT with it, mints an
installation token for the loop's repository only, and uses that token exactly as it would a PAT
(``gh.token``). What M0 measured (docs/github-app-support.md) shapes the rest:

* an installation token cannot call ``GET /user``, so the principal is the App's bot user, found
  from ``GET /app`` (who signed) and ``GET /users/<slug>[bot]`` (its id);
* tokens are opaque (390 characters, ``ghs_``) and live an hour: one is reused until ten minutes
  before it expires, by every host process (``app_tokens``, #1780), since GitHub's ETags follow
  the token and a token per process made every conditional read a counted 200;
* the JWT is signed by the Hermes runtime's Python with ``cryptography`` (the gateway's own
  interpreter may not have it), else by the ``openssl`` command; neither means App mode is
  unavailable, and every call says so instead of guessing.

No key, JWT or token is ever logged, raised or returned in an error.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import app_tokens, gh, util

JWT_TTL_S = 540                 # GitHub allows at most 10 minutes; the clock may run ahead
REUSE_MARGIN_S = 600            # a token is replaced ten minutes before it expires
SIGN_TIMEOUT_S = 20

# Run by the Hermes runtime's Python: the key path is an argument, the bytes to sign arrive on
# stdin, the signature leaves on stdout. Nothing else is read or printed.
# What signing imports, and so what the probe must import: a bare ``import cryptography`` passes on
# an install whose hazmat layer (its native bindings) is broken, and then every mint fails.
_PROBE = ("from cryptography.hazmat.primitives import hashes, serialization\n"
          "from cryptography.hazmat.primitives.asymmetric import padding\n")
_SIGN = ("import sys\n" + _PROBE +
         "with open(sys.argv[1], 'rb') as fh:\n"
         "    key = serialization.load_pem_private_key(fh.read(), password=None)\n"
         "sys.stdout.buffer.write(key.sign(sys.stdin.buffer.read(), padding.PKCS1v15(), "
         "hashes.SHA256()))\n")

_lock = threading.Lock()
_tokens: dict = {}              # (login, repo, app_id) -> (token, expires_at epoch)
_refused: set = set()           # tokens GitHub answered 401 in this process: never used again
_shared: set = set()            # keys whose token in _tokens is the shared one (app_tokens)
_principals: dict = {}          # (login, app_id) -> account


class AppError(gh.GitHubError):
    """An App credential could not be used. The message never carries a key or token."""


def entry(loop: dict, login: str | None) -> dict | None:
    """The ``apps`` entry for ``login`` (exact, then case-insensitive), or ``None``."""
    found = util.login_entry(loop.get("apps"), login)
    return found if isinstance(found, dict) else None


def is_app(loop: dict, login: str | None) -> bool:
    """Whether ``login`` is a GitHub App identity: ``config.is_app``, the one rule (#1254). A
    login mapped in ``tokens`` is a PAT even if ``apps`` maps it too (normalize refuses such a
    loop), so every caller classifies one login the same way; ``entry`` still finds its App."""
    from . import config
    return config.is_app(loop, login or "")


def key_path(loop: dict, login: str | None) -> Path | None:
    found = entry(loop, login)
    return Path(str(found["key_file"])).expanduser() if found and found.get("key_file") else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _runtime_python() -> Path | None:
    """The Hermes runtime venv's interpreter, from the private runtime file, if it exists."""
    from . import config, seat_model
    try:
        venv = seat_model.load_runtime(config.host_path("runtime")).get("venv")
    except Exception:
        return None
    python = Path(str(venv or "")) / "bin" / "python"
    return python if venv and python.is_file() else None


_signer: tuple[str, Path | None] | None = None      # this process's answer (#1187)


def _sign_timeout(what: str) -> float:
    """A signing subprocess's timeout: ``SIGN_TIMEOUT_S``, or what is left of a gate's budget
    (#1187: a mint is a gate's first call, and its budget may be smaller than one signing)."""
    left = gh.remaining()
    if left is None:
        return SIGN_TIMEOUT_S
    if left <= 0:
        raise gh.GateBudgetExceeded(f"time budget spent before {what}")
    return max(0.05, min(SIGN_TIMEOUT_S, left))


def signer() -> tuple[str, Path | None]:
    """``("cryptography", python)``, ``("openssl", path)`` or ``("", None)``: how this host can
    sign an App JWT. ``doctor`` reports it; ``sign`` uses it. Probed once per process (#1187):
    the runtime Python does not change under a running host; ``forget`` re-probes."""
    global _signer
    if _signer is not None:
        return _signer
    found: tuple[str, Path | None] = ("", None)
    cut_short = False
    python = _runtime_python()
    if python is not None:
        try:
            done = subprocess.run([str(python), "-I", "-c", _PROBE],
                                  capture_output=True, timeout=_sign_timeout("the signer probe"),
                                  env={"PATH": "/usr/bin:/bin"})
            if done.returncode == 0:
                found = ("cryptography", python)
        except subprocess.TimeoutExpired:
            cut_short = True        # a budget's limit, not an answer (#1278)
        except (OSError, subprocess.SubprocessError):
            pass
    if not found[0]:
        openssl = _openssl()
        found = ("openssl", openssl) if openssl else ("", None)
    if not cut_short:
        # A probe a tight gate budget cut short is not cached: it would answer "no signer" for
        # the process's life on a host whose runtime Python has cryptography (#1278).
        _signer = found
    return found


def _openssl() -> Path | None:
    found = shutil.which("openssl", path="/usr/bin:/bin:/usr/local/bin")
    return Path(found) if found else None


def sign(key_file: Path, data: bytes) -> bytes:
    """RS256 (PKCS#1 v1.5 over SHA-256) of ``data`` with the PEM key at ``key_file``. A
    ``cryptography`` signing that fails falls back to ``openssl`` once, and a fallback that signs
    becomes this process's signer: a probe can pass where signing still fails."""
    global _signer
    kind, tool = signer()
    try:
        return _sign_with(kind, tool, key_file, data)
    except SignTimeout:
        raise           # a signing cut short is no answer to retry: the retry would spend the
                        # rest of the budget and hide which signer failed (#2029)
    except AppError:
        fallback = _openssl() if kind == "cryptography" else None
        if fallback is None:
            raise
    signed = _sign_with("openssl", fallback, key_file, data)
    _signer = ("openssl", fallback)
    return signed


def _sign_with(kind: str, tool: Path | None, key_file: Path, data: bytes) -> bytes:
    if kind == "cryptography":
        argv = [str(tool), "-I", "-c", _SIGN, str(key_file)]
    elif kind == "openssl":
        argv = [str(tool), "dgst", "-sha256", "-sign", str(key_file)]
    else:
        raise AppError("no App signer on this host: the Hermes runtime's Python lacks "
                       "`cryptography` and there is no `openssl` command")
    try:
        done = subprocess.run(argv, input=data, capture_output=True,
                              timeout=_sign_timeout("App JWT signing"),
                              env={"PATH": "/usr/bin:/bin"})
    except subprocess.TimeoutExpired:
        left = gh.remaining()
        if left is not None and left <= 0:
            raise gh.GateBudgetExceeded("App JWT signing did not finish within the time "
                                        "budget") from None
        raise SignTimeout(f"App JWT signing failed ({kind} TimeoutExpired)") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppError(f"App JWT signing failed ({type(exc).__name__})") from None
    if done.returncode != 0 or not done.stdout:
        raise AppError(f"App JWT signing failed ({kind} exit {done.returncode}; check the key "
                       "file is the App's PEM private key)")
    return done.stdout


class SignTimeout(AppError):
    """A signing subprocess that ran out of time: reported, never retried with another signer."""


def jwt(loop: dict, login: str, now: float | None = None) -> str:
    found = entry(loop, login)
    if not found:
        raise AppError(f"{login!r} is not an App identity on this loop")
    stamp = int(time.time() if now is None else now)
    signing = (_b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
               + "." + _b64(json.dumps({"iat": stamp - 60, "exp": stamp + JWT_TTL_S,
                                        "iss": str(found["app_id"])},
                                       separators=(",", ":")).encode()))
    return signing + "." + _b64(sign(Path(str(found["key_file"])).expanduser(), signing.encode()))


def _as_app(loop: dict, login: str, path: str, method: str = "GET", body=None) -> gh.Response:
    """One REST call authenticated as the App itself (its JWT), not as an installation."""
    found = entry(loop, login) or {}
    if gh.envnames.get("GH_STUB"):
        return gh._stub(path, method, body, f"app:{found.get('app_id')}")
    import urllib.error
    import urllib.request
    from .config.host_guards import guard_network
    req = urllib.request.Request(
        f"{gh.API}{path}", data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"Accept": "application/vnd.github+json",
                                "Authorization": f"Bearer {jwt(loop, login)}",
                                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diaktoros"})
    guard_network(req.full_url)
    from . import gh_calls
    who = gh_calls.app_login(login)     # counted like every gh call (#1765), in its own bucket
    # Charged to a gate's budget like every gh call (#1187): its timeout is what is left, and a
    # failure past the deadline is an overrun, not an answer.
    timeout = gh._budgeted(method, path)
    answered, status, headers = False, None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            answered, status = True, resp.status
            gh_calls.record(who, method, path, gh_calls.outcome(path, resp.status), headers)
            return gh.Response(json.loads(resp.read().decode() or "null"), "", resp.status,
                               headers)
    except urllib.error.HTTPError as exc:
        try:
            detail = gh.one_line(exc.read().decode(errors="replace"), 120)
        finally:
            exc.close()
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        gh_calls.record(who, method, path, gh_calls.outcome(path, exc.code, False, headers),
                        headers)
        return gh._outcome(method, path, gh.Response(
            None, f"HTTP {exc.code}{f' {detail}' if detail else ''}", exc.code, {}))
    except Exception as exc:
        if not answered:                                # no HTTP answer: it may have counted
            gh_calls.record(who, method, path, "error")
        # A body that failed to read or parse follows an HTTP answer: its status and headers stay
        # on the response, so "no status" keeps meaning "no HTTP answer" (the doctor's UNKNOWN).
        return gh._outcome(method, path, gh.Response(None, f"{type(exc).__name__}: {exc}",
                                                     status, headers))


def _expires(stamp) -> float | None:
    return util.iso_epoch(stamp)


def installation_token(loop: dict, login: str) -> str:
    """An installation token for the loop's repository only, minted with the App's key and
    reused until ``REUSE_MARGIN_S`` before it expires. Raises ``AppError`` (never the token)."""
    found = entry(loop, login)
    if not found:
        raise AppError(f"{login!r} is not an App identity on this loop")
    repo = str(loop.get("repo") or "")
    key = (login.casefold(), repo, found.get("app_id"))
    warm = _reuse(key)
    if warm:
        return warm
    # One minter at a time for this token, across the host (#1780), each caller waiting within
    # its own bound: another App, or another thread's slower mint, never holds it (review F6).
    with app_tokens.held(key) as locked:
        shared = app_tokens.load(key)
        if shared and _keep(key, shared, shared=True):
            return shared[0]
        token, expires = _mint(loop, login, repo)
        _keep(key, (token, expires), shared=locked)
        if locked:                          # unlocked, it is this process's alone (F5)
            app_tokens.save(key, token, expires)
        return token


def _reuse(key: tuple) -> str | None:
    """This process's token for ``key`` while it is still good: fresh, never refused, and, when
    it is the shared one, still the token the shared file holds (review of #1781, F9). Another
    process that got a 401 drops the file, or replaces it, and this process follows at its next
    call instead of sending the dead token. None sends the caller to the locked path."""
    with _lock:
        cached = _tokens.get(key)
        shared = key in _shared
    if not cached or cached[1] - time.time() <= REUSE_MARGIN_S or cached[0] in _refused:
        return None
    if shared:
        stored = app_tokens.load(key)
        if not stored or stored[0] != cached[0]:
            return None
    return cached[0]


def _keep(key: tuple, token: tuple, *, shared: bool) -> bool:
    """Make ``token`` this process's for ``key`` unless it was refused or is not fresh, checked
    and stored in one step under ``_lock``, so a 401 recorded by another thread meanwhile is
    never undone (review of #1781, F8). Whether it was kept."""
    with _lock:
        if token[1] - time.time() <= REUSE_MARGIN_S or token[0] in _refused:
            return False
        _tokens[key] = token
        (_shared.add if shared else _shared.discard)(key)
        return True


def _mint(loop: dict, login: str, repo: str) -> tuple[str, float]:
    """A new installation token for ``repo`` and when it expires. Raises ``AppError``."""
    where = _as_app(loop, login, f"/repos/{repo}/installation")
    installation = where.data.get("id") if isinstance(where.data, dict) else None
    if type(installation) is not int:
        raise AppError(f"App {login} is not installed on {repo} ({where.error or 'no id'})")
    minted = _as_app(loop, login, f"/app/installations/{installation}/access_tokens",
                     method="POST", body={"repositories": [repo.split("/", 1)[-1]]})
    got = minted.data if isinstance(minted.data, dict) else {}
    token, expires = got.get("token"), _expires(got.get("expires_at"))
    if not isinstance(token, str) or not token or expires is None:
        raise AppError(f"App {login} could not mint a token for {repo} "
                       f"({minted.error or 'no token in the answer'})")
    return token, expires


def refused(loop: dict, login: str | None, sent: str) -> None:
    """GitHub answered 401 to ``sent``, the token a request carried as ``login``: if it is an
    App's, this process never uses it again, and it is dropped wherever it is still the current
    one, so the next call mints a new one instead of reusing a dead one (#1780).

    Only ``sent`` (review of #1781, F4): a late 401 for an old token never drops the successor
    already minted, here or by another process. Remembered even where the shared file cannot be
    written (F2), so a process that writes nothing stops reloading it."""
    found = entry(loop, login)
    if not found or not sent:
        return
    key = (str(login).casefold(), str(loop.get("repo") or ""), found.get("app_id"))
    with _lock:
        _refused.add(sent)
        if _tokens.get(key, ("",))[0] == sent:
            del _tokens[key]
    # Marked first, before any wait: every process reads the token as gone even if this one
    # never gets the lock, or its gate's budget runs out waiting for it (review F10).
    app_tokens.refuse(key, sent)
    with app_tokens.held(key) as locked:
        if not locked:                      # load-compare-drop is atomic only under the lock (F5)
            return
        stored = app_tokens.load(key)
        if stored and stored[0] == sent:
            app_tokens.drop(key)


def principal(loop: dict, login: str, fetch=None) -> dict | None:
    """The App's bot user (login, numeric id, ``type: Bot``) when the key in ``apps`` belongs to
    the App ``login`` names; ``None`` when it cannot be read. A key for another App answers with
    that App's bot, so the caller's own login check refuses it. ``/app`` is always read with the
    App's JWT; ``fetch(path)``, when given, is the caller's transport for the ``/users/<slug>[bot]``
    read (so its size bound and its refusal apply), and it may raise. A principal already
    resolved for this App is cached; with no ``fetch`` it is answered from the cache, but a caller
    that supplies ``fetch`` always has its transport run for the bot-user read (the cached slug
    saves the ``/app`` call), so a warm cache never bypasses its size bound or its refusal."""
    found = entry(loop, login)
    if not found:
        return None
    cache = (login.casefold(), found.get("app_id"))
    cached = _principals.get(cache)
    if cached and fetch is None:
        return dict(cached)
    if cached:
        app = {"id": cached["app_id"], "slug": cached["slug"]}
    else:
        try:
            app = _as_app(loop, login, "/app").data
        except AppError:
            return None
        if not isinstance(app, dict) or not isinstance(app.get("slug"), str) \
                or app.get("id") != found.get("app_id"):
            return None
    path = f"/users/{app['slug']}[bot]"
    user = fetch(path) if fetch is not None else gh.api(loop, path, login=login)
    if not isinstance(user, dict) or type(user.get("id")) is not int or user.get("type") != "Bot":
        return None
    account = {"login": user.get("login"), "id": user["id"], "type": "Bot", "app_id": app["id"],
               "slug": app["slug"]}
    _principals[cache] = account
    return dict(account)


def forget() -> None:
    """Drop every cached token and principal (tests, or after a key is rotated)."""
    global _signer
    with _lock:
        _tokens.clear()
        _refused.clear()
        _shared.clear()
        _principals.clear()
        _signer = None
        app_tokens.drop()
