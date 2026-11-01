"""GitHub App identities (#1163 M1b): a seat or the reader as an App's bot instead of a person.

A loop names an App identity in ``apps`` instead of ``tokens``::

    "apps": {"acme-reviewer[bot]": {"app_id": 5416187, "key_file": "/abs/acme-reviewer.pem"}}

The key stays on the host. For each GitHub call the host signs a short App JWT with it, mints an
installation token for the loop's repository only, and uses that token exactly as it would a PAT
(``gh.token``). What M0 measured (docs/github-app-support.md) shapes the rest:

* an installation token cannot call ``GET /user``, so the principal is the App's bot user, found
  from ``GET /app`` (who signed) and ``GET /users/<slug>[bot]`` (its id);
* tokens are opaque (390 characters, ``ghs_``) and live an hour: one is reused until ten minutes
  before it expires, per process;
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

from . import gh, util

JWT_TTL_S = 540                 # GitHub allows at most 10 minutes; the clock may run ahead
REUSE_MARGIN_S = 600            # a token is replaced ten minutes before it expires
SIGN_TIMEOUT_S = 20

# Run by the Hermes runtime's Python: the key path is an argument, the bytes to sign arrive on
# stdin, the signature leaves on stdout. Nothing else is read or printed.
_SIGN = ("import sys\n"
         "from cryptography.hazmat.primitives import hashes, serialization\n"
         "from cryptography.hazmat.primitives.asymmetric import padding\n"
         "with open(sys.argv[1], 'rb') as fh:\n"
         "    key = serialization.load_pem_private_key(fh.read(), password=None)\n"
         "sys.stdout.buffer.write(key.sign(sys.stdin.buffer.read(), padding.PKCS1v15(), "
         "hashes.SHA256()))\n")

_lock = threading.Lock()
_tokens: dict = {}              # (login, repo, app_id) -> (token, expires_at epoch)
_principals: dict = {}          # (login, app_id) -> account


class AppError(gh.GitHubError):
    """An App credential could not be used. The message never carries a key or token."""


def entry(loop: dict, login: str | None) -> dict | None:
    """The ``apps`` entry for ``login`` (exact, then case-insensitive), or ``None``."""
    found = util.login_entry(loop.get("apps"), login)
    return found if isinstance(found, dict) else None


def is_app(loop: dict, login: str | None) -> bool:
    return entry(loop, login) is not None


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
            done = subprocess.run([str(python), "-I", "-c", "import cryptography"],
                                  capture_output=True, timeout=_sign_timeout("the signer probe"),
                                  env={"PATH": "/usr/bin:/bin"})
            if done.returncode == 0:
                found = ("cryptography", python)
        except subprocess.TimeoutExpired:
            cut_short = True        # a budget's limit, not an answer (#1278)
        except (OSError, subprocess.SubprocessError):
            pass
    if not found[0]:
        openssl = shutil.which("openssl", path="/usr/bin:/bin:/usr/local/bin")
        found = ("openssl", Path(openssl)) if openssl else ("", None)
    if not cut_short:
        # A probe a tight gate budget cut short is not cached: it would answer "no signer" for
        # the process's life on a host whose runtime Python has cryptography (#1278).
        _signer = found
    return found


def sign(key_file: Path, data: bytes) -> bytes:
    """RS256 (PKCS#1 v1.5 over SHA-256) of ``data`` with the PEM key at ``key_file``."""
    kind, tool = signer()
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
        raise AppError("App JWT signing failed (TimeoutExpired)") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppError(f"App JWT signing failed ({type(exc).__name__})") from None
    if done.returncode != 0 or not done.stdout:
        raise AppError(f"App JWT signing failed ({kind} exit {done.returncode}; check the key "
                       "file is the App's PEM private key)")
    return done.stdout


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
    # Charged to a gate's budget like every gh call (#1187): its timeout is what is left, and a
    # failure past the deadline is an overrun, not an answer.
    timeout = gh._budgeted(method, path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return gh.Response(json.loads(resp.read().decode() or "null"), "", resp.status,
                               {k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        try:
            detail = gh.one_line(exc.read().decode(errors="replace"), 120)
        finally:
            exc.close()
        return gh._outcome(method, path, gh.Response(
            None, f"HTTP {exc.code}{f' {detail}' if detail else ''}", exc.code, {}))
    except Exception as exc:
        return gh._outcome(method, path, gh.Response(None, f"{type(exc).__name__}: {exc}"))


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
    with _lock:
        cached = _tokens.get(key)
        if cached and cached[1] - time.time() > REUSE_MARGIN_S:
            return cached[0]
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
        _tokens[key] = (token, expires)
        return token


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
        _principals.clear()
        _signer = None
