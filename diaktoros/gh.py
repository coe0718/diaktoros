"""GitHub REST access — stdlib only, token read from a file the operator controls.

Two deliberate choices:

* **No ``gh`` CLI dependency.** The gates run inside the gateway, where a PATH or a
  keyring is not guaranteed. A token file plus ``urllib`` works everywhere and has no
  login state to expire.
* **One token per seat.** Each seat's token lives in its own file (mode 600) and is named
  in the loop config, so the credential that pushes, the credential that reviews and the
  credential that reads are separate and revocable one at a time.

Test hook: set ``DIAKTOROS_GH_STUB`` to an executable that takes the API path as argv[1]
and prints a JSON response. That is how the test suite exercises the gates without a
network or a real repository. To answer with an HTTP status or response headers, the stub
prints ``{"__gh_stub_response__": {"status": 401, "headers": {...}, "body": ...}}``.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from .util import log
from . import envnames

API = "https://api.github.com"
REVIEW_PAGE_SIZE = 100
# A persistently full endpoint must not loop forever or authorize a partial history.
MAX_REVIEW_PAGES = 100
# The same bound for the open-PR listing: a repository past 10,000 open PRs reads as unknown.
MAX_PR_PAGES = 100
# GitHub's pulls/N/files listing stops at 3,000 files (30 full pages); the 31st page is what
# proves the listing ended, so a PR at GitHub's cap still reads as complete-as-GitHub-lists-it.
MAX_PR_FILE_PAGES = 31


class GitHubError(Exception):
    pass


class Response(NamedTuple):
    """One REST call, whole: ``status`` is None when no HTTP answer arrived at all."""
    data: object | None
    error: str
    status: int | None = None
    headers: dict | None = None

# The header GitHub sets on answers to fine-grained and expiring classic tokens.
TOKEN_EXPIRY_HEADER = "github-authentication-token-expiration"
STUB_ENVELOPE = "__gh_stub_response__"


def parse_token_expiry(value) -> float | None:
    """GitHub's expiry header (``2026-10-01 12:00:00 UTC``, or a numeric offset) as epoch."""
    from datetime import datetime, timezone
    text = str(value or "").strip().replace(" UTC", " +0000")
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


class GateBudgetExceeded(BaseException):
    """A gate (or the watchdog's sweep) ran out of its time budget (issue #75). A
    ``BaseException`` on purpose: the ``except Exception`` fallbacks that turn a failed read into
    "unknown → silence" must not swallow it, or an overrun would read as a deliberate
    ``[SILENT]`` again."""


# Set only while a gate runs under ``gate_failures.run`` (or the watchdog under its sweep
# budget): the monotonic deadline, the per-call cap, and every call that failed, so a silence
# that followed a failed read is told apart from a chosen one.
_GATE: dict = {"deadline": None, "errors": None, "per_call": 30.0}


def begin_gate(deadline: float, per_call: float = 30.0) -> None:
    _GATE.update(deadline=deadline, errors=[], per_call=per_call)


def end_gate() -> list[tuple[str, str, str]]:
    errors = _GATE.get("errors") or []
    _GATE.update(deadline=None, errors=None, per_call=30.0)
    return errors


def remaining() -> float | None:
    """Seconds left in the current budget, or ``None`` when no budget is set."""
    deadline = _GATE.get("deadline")
    return None if deadline is None else deadline - time.monotonic()


def _budgeted(method: str, path: str) -> float:
    """The per-call timeout: the per-call cap, or what is left of the budget. Raises when spent."""
    per_call = float(_GATE.get("per_call") or 30.0)
    left = remaining()
    if left is None:
        return per_call
    if left <= 0:
        raise GateBudgetExceeded(f"time budget spent before {method} {path}")
    return max(0.05, min(per_call, left))


def _outcome(method: str, path: str, response: "Response") -> "Response":
    """A failed call past the deadline is an overrun, not an answer; any other failure is noted
    for ``gate_failures`` (a silence after it is "incomplete", not a decision)."""
    if response.error:
        left = remaining()
        if left is not None and left <= 0:
            raise GateBudgetExceeded(f"{method} {path} did not finish within the time budget "
                                     f"({response.error})")
        if _GATE.get("errors") is not None:
            _GATE["errors"].append((method, path, response.error))
    return response


def token_path(loop: dict, login: str | None = None) -> pathlib.Path | None:
    name = login or loop.get("read_token")
    raw = (loop.get("tokens") or {}).get(name)
    return pathlib.Path(str(raw)).expanduser() if raw else None


def token(loop: dict, login: str | None = None) -> str:
    path = token_path(loop, login)
    if not path or not path.exists():
        raise GitHubError(f"no token file for {login or loop.get('read_token')!r} "
                          f"(checked {path})")
    return path.read_text().strip()


def _stub(path: str, method: str, body, login: str = "", timeout: float = 30.0) -> Response:
    """The stub executable's answer, shaped like a real one.

    ``(None, "")`` means the stub answered "no such resource" — the same shape ``api`` gives a
    real 404. An empty error and a non-empty one are therefore different facts, which is the
    whole reason this returns the error apart from the payload instead of ``None`` for both.
    """
    stub = envnames.get("GH_STUB")
    if not stub:
        return Response(None, "")
    argv = [stub, path] if not body else [stub, path, json.dumps(body)]
    # GH_LOGIN names the token login the call would use (never the token), so a stub can play
    # a read token that GitHub refuses a hook write.
    env = {**os.environ, "GH_METHOD": method, "GH_LOGIN": str(login or "")}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception as exc:
        return Response(None, f"gh stub failed: {exc}")
    if proc.returncode != 0:
        return Response(None, f"gh stub rc={proc.returncode}: {proc.stderr.strip()[:120]}")
    out = proc.stdout.strip()
    if not out:
        return Response(None, "gh stub printed nothing")
    try:
        data = json.loads(out)
    except Exception:
        return Response(None, "gh stub printed invalid JSON")
    envelope = data.get(STUB_ENVELOPE) if isinstance(data, dict) else None
    if not isinstance(envelope, dict):
        return Response(data, "", 200 if data is not None else None, {})
    status = envelope.get("status") if type(envelope.get("status")) is int else 200
    headers = {str(k).lower(): str(v) for k, v in (envelope.get("headers") or {}).items()}
    payload = envelope.get("body")
    if status >= 400:
        detail = json.dumps(payload)[:120] if payload is not None else ""
        return Response(None, f"HTTP {status}{f' {detail}' if detail else ''}", status, headers)
    return Response(payload, "", status, headers)


def request(loop: dict, path: str, method: str = "GET", body=None,
            login: str | None = None) -> Response:
    """One REST call with its HTTP status and response headers. No logging, no interpretation.

    ``fetch`` is this without the status: most callers only need "did it work". The watchdog's
    health check needs the rest — a 401 (the token is dead) and a 502 (GitHub is) ask different
    things of the operator, and the token's expiry arrives only as a response header.

    Budgeted (#75): inside a gate or a watchdog sweep each call gets at most what is left of the
    budget, and a call that fails because the budget ran out raises ``GateBudgetExceeded``.
    """
    timeout = _budgeted(method, path)
    return _outcome(method, path, _request(loop, path, method, body, login, timeout))


def _request(loop: dict, path: str, method: str, body, login: str | None,
             timeout: float) -> Response:
    if envnames.get("GH_STUB"):
        # The login travels to the stub (as GH_LOGIN, never a token) so a test can play a read
        # token GitHub refuses a hook write.
        return _stub(path, method, body, login or loop.get("read_token") or "", timeout)
    try:
        tok = token(loop, login)
    except GitHubError as exc:
        return Response(None, str(exc))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"token {tok}",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diaktoros"})
    from .config import guard_network
    guard_network(req.full_url)         # under the test guard: loopback fakes only
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "null"
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return Response(json.loads(raw), "", resp.status, headers)
    except urllib.error.HTTPError as exc:
        try:   # an HTTPError holds the response open: close it, not the collector
            detail = one_line(exc.read().decode(errors="replace"), 120)
        finally:
            exc.close()
        # Safe after close(): HTTPError.headers is a property over .hdrs, which close() leaves.
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return Response(None, f"HTTP {exc.code}{f' {detail}' if detail else ''}", exc.code, headers)
    except Exception as exc:
        return Response(None, f"{type(exc).__name__}: {exc}")


def fetch(loop: dict, path: str, method: str = "GET", body=None,
          login: str | None = None) -> tuple[object | None, str]:
    """One REST call as ``(payload, error)``. No logging, no interpretation.

    ``api`` is this call for callers that only need the payload and read every failure as
    "unknown". ``explain`` needs the difference: a 404 is a fact about the pull request (it is
    not there), a timeout is a fact about the network — and "check the number" and "retry the
    read" are not interchangeable answers to an operator at 2am.
    """
    response = request(loop, path, method, body, login)
    return response.data, response.error


class _TokenlessRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect without carrying the token to another host.

    urllib copies every header but the content ones onto a redirected request, ``Authorization``
    included, so GitHub's redirect from a job log to its log storage would hand the read token
    to that storage host. Here the token stays on the host it was meant for, an https request is
    never followed down to http, and the target passes the same network guard as the first URL.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        if before.scheme == "https" and after.scheme != "https":
            raise urllib.error.HTTPError(new.full_url, code, "refused: redirect leaves https",
                                         headers, fp)
        from .config import guard_network
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
        data = _stub(path, "GET", None, login or loop.get("read_token") or "").data
        data = data.get("text") if isinstance(data, dict) else data
        return data[-limit:] if isinstance(data, str) else None
    try:
        tok = token(loop, login or loop.get("read_token"))
        req = urllib.request.Request(
            f"{API}{path}", method="GET",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"token {tok}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diaktoros"})
        from .config import guard_network
        guard_network(req.full_url)
        with _TEXT_OPENER.open(req, timeout=_budgeted("GET", path)) as resp:
            # Only the tail matters: keep reading, keep the last ``limit`` bytes.
            kept = b""
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                kept = (kept + chunk)[-limit:]
            return kept.decode(errors="replace")
    except Exception as exc:
        log(f"gh GET {path} (text) failed: {type(exc).__name__}: {one_line(exc, 120)}")
        return None


def one_line(text, limit: int = 200) -> str:
    """``text`` as one bounded line: GitHub's error bodies are pretty-printed JSON, and an
    operator alert (or an ``explain`` line) promised as one line must stay one."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def write_outcome(method: str, path: str, status: int | None) -> str:
    """What a failed write means for the operator. A 4xx is GitHub refusing it: nothing
    happened. No answer, or a 5xx, leaves the outcome unknown — GitHub may have applied it
    before failing to answer — so name what to look at before anyone retries it."""
    if status is not None and 400 <= status < 500:
        return "GitHub refused it, so it did not take effect"
    number = re.search(r"/(?:pulls|issues)/(\d+)", path or "")
    where = f"PR #{number.group(1)}" if number else "the repo"
    what = ("the review request" if "requested_reviewers" in (path or "") else
            "the comment" if (path or "").endswith("/comments") else
            "the review" if (path or "").endswith("/reviews") else
            "the change")
    return (f"its outcome is unknown (GitHub may have applied it without answering) — check "
            f"{where} on GitHub for {what} before re-sending it")


def status_of(error: str) -> int | None:
    """The HTTP status an error string names (``fetch``'s ``HTTP 401 …``), or None."""
    match = re.search(r"\bHTTP (\d{3})\b", error or "")
    return int(match.group(1)) if match else None


def failure_hint(status: int | None) -> str:
    """What a failed read most likely means, for the one line an operator gets."""
    if status == 401:
        return "token expired or revoked?"
    if status == 403:
        return "token lacks access (scope, SSO) or is rate-limited?"
    if status == 404:
        return "token cannot see this resource (scope or repo access)?"
    if status is not None and status >= 500:
        return "GitHub is failing — outage?"
    if status is None:
        return "no HTTP answer — network, DNS, or the token file?"
    return ""


def auth_probe(loop: dict) -> Response:
    """``GET /user`` as the read token: who it is, and (in the headers) when it expires."""
    return request(loop, "/user")


_RECORD_FAILURES = False


def record_failures() -> None:
    """Opt this process in to ``record_failure``. The gates do (``gate.context``); ``explain`` and
    ``doctor`` never do, because they promise to write nothing."""
    global _RECORD_FAILURES
    _RECORD_FAILURES = True


def record_failure(loop: dict, method: str, path: str, error: str,
                   login: str | None = None) -> None:
    """Leave a failed call where the watchdog and ``explain`` can find it. Never raises.

    A gate that cannot read the current PR answers ``[SILENT]`` — correctly, since unknown is not
    permission — but the gateway's stderr is the only other witness. This keeps the last one on
    disk, in the loop's state directory, so "the event was dropped because GitHub was unreadable"
    is something the next sweep can say out loud.
    """
    if not _RECORD_FAILURES or not loop.get("state_dir") or status_of(error) == 404:
        return
    try:
        from . import state as state_mod
        state_mod.state_for(loop).github_failure_record({
            "at": time.time(), "where": pathlib.Path(sys.argv[0] or "review-loop").name,
            "method": method, "path": path.split("?", 1)[0], "error": one_line(error, 200),
            "status": status_of(error), "login": login or loop.get("read_token") or ""})
    except Exception:
        pass


def api(loop: dict, path: str, method: str = "GET", body=None, login: str | None = None):
    """One REST call. Returns parsed JSON, or None when the call did not succeed.

    Callers are expected to treat None as "unknown" and stay quiet: a loop that cannot
    read the review list must not guess how many rounds are left.
    """
    data, error = fetch(loop, path, method, body, login)
    _LAST.error = error
    if error:
        log(f"gh {method} {path} failed: {error}")
        record_failure(loop, method, path, error, login)
    return data


_LAST = threading.local()


def last_error() -> str:
    """The error of this thread's latest ``api`` call (empty when it succeeded): lets a writer
    that must refuse on None say why without a second request."""
    return getattr(_LAST, "error", "")


# -- convenience --------------------------------------------------------------
#
# The paths live in one place each: a second copy of "/repos/{repo}/pulls/{n}" is exactly the
# kind of thing that drifts between a gate and the command that explains the gate.


def pr_path(loop: dict, number: int) -> str:
    return f"/repos/{loop['repo']}/pulls/{number}"


def reviews_path(loop: dict, number: int) -> str:
    return f"{pr_path(loop, number)}/reviews?per_page=100"


def hooks_path(loop: dict) -> str:
    return f"/repos/{loop['repo']}/hooks?per_page=100"


def hooks_read(loop: dict, login: str | None = None) -> tuple[list[dict] | None, str]:
    """Every repo hook (all pages), or ``(None, reason)`` — never a prefix of the listing."""
    return _read_pages(loop, hooks_path(loop), "hook", MAX_PR_PAGES, login=login)


def pr(loop: dict, number: int):
    return api(loop, pr_path(loop, number))


def pr_url(loop: dict, number: int) -> str:
    """The pull request's canonical web URL — the one link a human needs.

    Lives here rather than in the gate because two very different things need it: the prompts
    that wake a seat, and the observer's notices. It is GitHub's address space, not either
    caller's.
    """
    return f"https://github.com/{loop['repo']}/pull/{number}"


def issue_url(loop: dict, number: int) -> str:
    """An issue's canonical web URL: triage and issue-fix notices link here, not to /pull/."""
    return f"https://github.com/{loop['repo']}/issues/{number}"


def reviews_read(loop: dict, number: int) -> tuple[list[dict] | None, str]:
    """Read the entire review history, or return unknown without partial results.

    A full page does not prove it is the last page. The first path remains unchanged for
    existing API stubs; subsequent pages use GitHub's ordinary page query parameter.
    """
    return _read_pages(loop, reviews_path(loop, number), "review", MAX_REVIEW_PAGES)


def _read_pages(loop: dict, path: str, what: str, max_pages: int,
                login: str | None = None) -> tuple[list[dict] | None, str]:
    """Every page of a ``per_page=100`` listing, or ``(None, reason)`` — never a prefix of it.

    A failed, malformed or oversized page anywhere makes the whole listing unknown: the caller
    would otherwise read "the first N items" as "all of them".
    """
    result: list[dict] = []
    for page in range(1, max_pages + 1):
        page_path = path if page == 1 else f"{path}&page={page}"
        items, error = fetch(loop, page_path, login=login) if login else fetch(loop, page_path)
        if error:
            return None, f"{what} page {page}: {error}"
        if not isinstance(items, list) or len(items) > REVIEW_PAGE_SIZE or not all(
                isinstance(item, dict) for item in items):
            return None, f"{what} page {page}: invalid {what} list"
        result.extend(items)
        if len(items) < REVIEW_PAGE_SIZE:
            return result, ""
    return None, f"{what} listing exceeds {max_pages} full pages"


# Issue comments on a PR, read in full for the seats' records (the fixer's answers, #52).
MAX_COMMENT_PAGES = 30


def issue_comments_read(loop: dict, number: int) -> tuple[list[dict] | None, str]:
    """Every issue comment on the PR, or ``(None, reason)`` — never the oldest page alone."""
    return _read_pages(loop, f"/repos/{loop['repo']}/issues/{number}/comments?per_page=100",
                       "comment", MAX_COMMENT_PAGES)


def reviews(loop: dict, number: int, errors: list | None = None):
    """The PR's whole review history, or ``None`` (unknown). Pass ``errors`` to receive why."""
    result, error = reviews_read(loop, number)
    if error:
        log(f"gh GET {reviews_path(loop, number)} failed: {one_line(error)}")
        record_failure(loop, "GET", reviews_path(loop, number), error)
        if errors is not None:
            errors.append(error)
    return result


# GitHub answered, and the answer is no: retrying will not change it (#110). A 403 that is a rate
# limit is the exception — that one passes.
_PERSISTENT_HTTP = (403, 404, 410)


def persistent_failure(error: str) -> bool:
    """Whether a listing error (``_read_pages``' reason) is an answer rather than an outage.

    404/410/403 (other than a rate limit), a malformed page and a listing past its page bound are
    answers; a 5xx, 429, timeout or connection error is transient, and so is anything unknown.
    """
    import re
    text = str(error)
    match = re.search(r"\bHTTP (\d{3})\b", text)
    if match:
        code = int(match.group(1))
        return code in _PERSISTENT_HTTP and not (code == 403 and "rate limit" in text.lower())
    return "invalid " in text or "listing exceeds" in text


def pr_files_read(loop: dict, number: int) -> tuple[list[dict] | None, str]:
    """Every changed file GitHub lists for the PR (``pulls/N/files``), or ``(None, reason)``.

    Read with the loop's read token, like every other listing here. GitHub itself stops listing
    at 3,000 files; a PR that large reads as its first 3,000 and the caller says so.
    """
    path = f"{pr_path(loop, number)}/files?per_page=100"
    return _read_pages(loop, path, "PR file", MAX_PR_FILE_PAGES)


def open_prs_read(loop: dict) -> tuple[list[dict] | None, str]:
    """Complete bounded listing: a full last page cannot authorize a partial chain."""
    path = f"/repos/{loop['repo']}/pulls?state=open&per_page=100"
    return _read_pages(loop, path, "open PR", MAX_PR_PAGES)


def open_prs(loop: dict, errors: list | None = None):
    """Every open PR, or ``None`` (unknown) when any page could not be read.

    The watchdog treats this as its scheduling view; a repository with more than 100 open PRs
    read as "the first 100" would silently never scan or drain the rest. Pass ``errors`` to
    receive why (the watchdog alerts on a failed listing like on any failed read).
    """
    result, error = open_prs_read(loop)
    if error:
        log(f"gh open PR list failed: {one_line(error)}")
        if errors is not None:
            errors.append(error)
    return result



def request_review(loop: dict, number: int, login: str | None = None, as_login: str | None = None):
    """Ask for a review explicitly.

    GitHub clears a pending review request the moment a review is submitted, so the fixer
    must re-ask after every push — this call is what keeps the loop turning.
    """
    seat = login or loop["reviewer_seat"]
    return api(loop, f"/repos/{loop['repo']}/pulls/{number}/requested_reviewers",
               method="POST", body={"reviewers": [seat]}, login=as_login)


def review_state(review: dict) -> str:
    """Webhook payloads spell review states lowercase; the REST API shouts them.

    Comparing case-insensitively on both sides of that boundary is not paranoia: the first
    version of this loop had a fixer leg that was dead on arrival because of exactly this.
    """
    return str((review or {}).get("state", "")).upper()


def gh_cli_available() -> bool:
    return shutil.which("gh") is not None
