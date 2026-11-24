"""A plain-text GitHub read: an Actions job log, followed through GitHub's redirect without the
token (split out of ``gh``, which sits at its size ceiling).

GitHub answers a log request with a redirect to its log storage. ``read_text`` follows it with
``_TokenlessRedirect``, keeps only the log's tail, and counts the call once whatever the storage
host does (``gh_calls``). The token, the API root, the gate budget and the test stub are ``gh``'s,
read through the module at call time, so a patch on ``gh`` reaches this read too.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

from . import envnames, gh
from .util import log


class _TokenlessRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect without carrying the token to another host.

    urllib copies every header but the content ones onto a redirected request, ``Authorization``
    included, so GitHub's redirect from a job log to its log storage would hand the read token
    to that storage host. Here the token stays on the host it was meant for, an https request is
    never followed down to http, and the target passes the same network guard as the first URL.
    The answer's headers are kept on the request as ``answered``: GitHub answered, so the call is
    spent whatever the storage host does next (``read_text`` counts it).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not hasattr(req, "answered"):
            req.answered = {k.lower(): v for k, v in headers.items()}
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        if before.scheme == "https" and after.scheme != "https":
            raise urllib.error.HTTPError(new.full_url, code, "refused: redirect leaves https",
                                         headers, fp)
        from .config.host_guards import guard_network
        guard_network(new.full_url)
        if (after.hostname, after.port) != (before.hostname, before.port):
            new.remove_header("Authorization")
        return new


_TEXT_OPENER = urllib.request.build_opener(_TokenlessRedirect)


def read_text(loop: dict, path: str, login: str | None = None, limit: int = 262144) -> str | None:
    """A plain-text body (an Actions job log), at most its last ``limit`` bytes, or None.

    GitHub answers a log request with a redirect to the file in its log storage; the redirect is
    followed without the token (``_TokenlessRedirect``). The read is bounded, so a huge log never
    lands in memory whole. Under the test stub, a stub that prints a JSON string (or
    ``{"text": ...}``) stands in for the log.
    """
    if envnames.get("GH_STUB"):
        data = gh._stub(path, "GET", None, login or loop.get("read_token") or "").data
        data = data.get("text") if isinstance(data, dict) else data
        return data[-limit:] if isinstance(data, str) else None
    from . import gh_calls
    who, counted, req = str(login or loop.get("read_token") or ""), False, None
    try:
        try:
            tok = gh.token(loop, login or loop.get("read_token"))
            timeout = gh._budgeted("GET", path)
        except Exception:
            counted = True       # no credential, or the gate's budget is spent: nothing was sent
            gh_calls.record(who, "GET", path, "unsent")
            raise
        req = urllib.request.Request(
            f"{gh.API}{path}", method="GET",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"token {tok}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diaktoros"})
        from .config.host_guards import guard_network
        guard_network(req.full_url)
        with _TEXT_OPENER.open(req, timeout=timeout) as resp:
            counted = True
            gh_calls.record(who, "GET", path, "spent", getattr(req, "answered", None))
            # Only the tail matters: keep reading, keep the last ``limit`` bytes.
            kept = b""
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                kept = (kept + chunk)[-limit:]
            return kept.decode(errors="replace")
    except Exception as exc:
        if not counted:      # each read is counted once: a refusal (a limited 403) or no answer
            _count_failed_text(who, path, exc, getattr(req, "answered", None))
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()      # it holds the response open; its headers were read above
            if getattr(req, "answered", None) is None:      # GitHub's own answer, not storage's
                gh.refusal(loop, who, tok, exc.code)
        log(f"gh GET {path} (text) failed: {type(exc).__name__}: {gh.one_line(exc, 120)}")
        return None


def _count_failed_text(who: str, path: str, exc: Exception, answered: dict | None) -> None:
    """Count a log read that failed. Once GitHub has answered with its redirect the API call is
    spent, whatever the storage host did after: its refusal is not GitHub's rate limit."""
    from . import gh_calls
    if answered is not None:
        gh_calls.record(who, "GET", path, "spent", answered)
        return
    headers = {k.lower(): v for k, v in (getattr(exc, "headers", None) or {}).items()}
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    gh_calls.record(who, "GET", path, gh_calls.outcome(path, status, False, headers), headers)
