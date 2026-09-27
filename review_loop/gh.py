"""GitHub REST access — stdlib only, token read from a file the operator controls.

Two deliberate choices:

* **No ``gh`` CLI dependency.** The gates run inside the gateway, where a PATH or a
  keyring is not guaranteed. A token file plus ``urllib`` works everywhere and has no
  login state to expire.
* **One token per seat.** Each seat's token lives in its own file (mode 600) and is named
  in the loop config, so the credential that pushes, the credential that reviews and the
  credential that reads are separate and revocable one at a time.

Test hook: set ``REVIEW_LOOP_GH_STUB`` to an executable that takes the API path as argv[1]
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
import time
import urllib.error
import urllib.request
from typing import NamedTuple

from .util import log

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


def _stub(path: str, method: str, body) -> Response:
    """The stub executable's answer, shaped like a real one.

    ``(None, "")`` means the stub answered "no such resource" — the same shape ``api`` gives a
    real 404. An empty error and a non-empty one are therefore different facts, which is the
    whole reason this returns the error apart from the payload instead of ``None`` for both.
    """
    stub = os.environ.get("REVIEW_LOOP_GH_STUB")
    if not stub:
        return Response(None, "")
    argv = [stub, path] if not body else [stub, path, json.dumps(body)]
    env = {**os.environ, "GH_METHOD": method}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
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
    """
    if os.environ.get("REVIEW_LOOP_GH_STUB"):
        return _stub(path, method, body)
    try:
        tok = token(loop, login)
    except GitHubError as exc:
        return Response(None, str(exc))
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"token {tok}",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-review-loop"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "null"
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return Response(json.loads(raw), "", resp.status, headers)
    except urllib.error.HTTPError as exc:
        detail = one_line(exc.read().decode(errors="replace"), 120)
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
    if error:
        log(f"gh {method} {path} failed: {error}")
        record_failure(loop, method, path, error, login)
    return data


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


def pr(loop: dict, number: int):
    return api(loop, pr_path(loop, number))


def pr_url(loop: dict, number: int) -> str:
    """The pull request's canonical web URL — the one link a human needs.

    Lives here rather than in the gate because two very different things need it: the prompts
    that wake a seat, and the observer's notices. It is GitHub's address space, not either
    caller's.
    """
    return f"https://github.com/{loop['repo']}/pull/{number}"


def reviews_read(loop: dict, number: int) -> tuple[list[dict] | None, str]:
    """Read the entire review history, or return unknown without partial results.

    A full page does not prove it is the last page. The first path remains unchanged for
    existing API stubs; subsequent pages use GitHub's ordinary page query parameter.
    """
    return _read_pages(loop, reviews_path(loop, number), "review", MAX_REVIEW_PAGES)


def _read_pages(loop: dict, path: str, what: str, max_pages: int) -> tuple[list[dict] | None, str]:
    """Every page of a ``per_page=100`` listing, or ``(None, reason)`` — never a prefix of it.

    A failed, malformed or oversized page anywhere makes the whole listing unknown: the caller
    would otherwise read "the first N items" as "all of them".
    """
    result: list[dict] = []
    for page in range(1, max_pages + 1):
        page_path = path if page == 1 else f"{path}&page={page}"
        items, error = fetch(loop, page_path)
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


def reviews(loop: dict, number: int):
    result, error = reviews_read(loop, number)
    if error:
        log(f"gh GET {reviews_path(loop, number)} failed: {error}")
        record_failure(loop, "GET", reviews_path(loop, number), error)
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
