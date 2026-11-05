#!/usr/bin/env python3
"""GitHub App feasibility spike (#1164): what three App identities can actually do on GitHub.

Run against a throwaway repository only. It needs three GitHub Apps (reader, reviewer, fixer),
each installed on that repository alone, and their private keys in 0600 files. It opens PRs,
pushes, reviews and requests reviewers there, then prints one JSON line per step. The results
are written up in docs/github-app-support.md.

    python3 tools/github_app_spike.py --repo OWNER/REPO \\
        --app reader=ID --app reviewer=ID --app fixer=ID --keys ~/.hermes/dk-spike \\
        [--check-branch BRANCH] [--person-token-env NAME]

Row 6 (a person's PR) needs a person's token, which no App can stand in for: put a personal access
token with write access to the repository in the environment variable named by
``--person-token-env`` (default ``SPIKE_PERSON_TOKEN``). Without it the step prints ``skipped``.
The repository, its branch protection and the Apps are set up by hand; the harness creates none
of them (an App has no Administration permission). The second protected branch (no required
approvals) is the one named by ``--check-branch``.

A step that gets an answer it cannot go on without (an App's identity, a file, a branch, a pull
request, a commit) ends the run: the last line printed is ``{"step": ..., "status": ..., "error":
...}`` and the exit code is 1, so a failed run still says where it stopped.

Standard library plus the ``openssl`` CLI (App JWTs are RS256). No key or token is ever printed:
a token is described by its length, prefix and expiry only. The repository's ``main`` should be
protected with one required approval, so step ``protection`` can say whether an App counts.
Not part of the plugin: nothing in diaktoros/ imports it.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"
ROLES = ("reader", "reviewer", "fixer")
PERSON_TOKEN_ENV = "SPIKE_PERSON_TOKEN"
# GitHub needs a few seconds after a push before its commit and merge state settle.
POST_PUSH_WAIT = 6


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


ERROR_LIMIT = 240  # characters of a failed step's ``error`` that reach the log


# What a credential looks like wherever GitHub or a proxy might echo it back into an answer:
# GitHub's own token prefixes (any length: a short one is still a token), an Authorization header
# value, a Bearer value, a ``token`` value, a JWT (an App's) and a PEM private key. What follows
# ``Authorization:`` and ``Bearer`` is a credential whatever its length: a proxy can echo a short
# one that this run does not hold. A bare ``token`` is also ordinary English ("a JSON web token
# could not be decoded"), so there only a value that has a digit or is 20+ characters is cut.
_CREDENTIAL_SHAPES = re.compile(
    r"\b(?:gh[pousr]_|github_pat_)\w+"
    r"|(?i:\bAuthorization\b[\"']?\s*[:=]\s*[\"']?(?:(?:token|bearer|basic)\s+)?[^\s\"',;]+)"
    r"|(?i:\bbearer\s+)[A-Za-z0-9_.~+/=-]+"
    r"|(?i:\btoken\s+)(?=[A-Za-z0-9_.~+/=-]*\d|[A-Za-z0-9_.~+/=-]{20})[A-Za-z0-9_.~+/=-]+"
    r"|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"
    r"|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----|[\s\S]*)")


def scrub(text: str, secrets=()) -> str:
    """``text`` with every credential-shaped piece, and every one of ``secrets`` (values this run
    holds; four characters or more), replaced by ``[REDACTED]``. What GitHub says goes into the
    log only through here: the docs promise that no token is printed."""
    for secret in sorted({s.strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= 4},
                         key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return _CREDENTIAL_SHAPES.sub("[REDACTED]", text)


class StepFailed(Exception):
    """A step got an API answer it cannot go on without: its name, GitHub's status, what it said."""

    def __init__(self, step: str, status: int, error: str):
        super().__init__(f"{step}: {status} {error}")
        self.step, self.status, self.error = step, status, error


def dig(body, path: str):
    """The value at the dotted ``path`` (``"commit.sha"``) in nested objects, else ``None``."""
    for part in path.split("."):
        body = body.get(part) if isinstance(body, dict) else None
    return body


def expect(step: str, got: tuple, *paths: str, what: str = ""):
    """The body of ``got`` (the ``(status, body)`` of ``Spike.gh`` / ``Spike.app``) when its status
    is 2xx and the body holds a value at every dotted one of ``paths``; otherwise ``StepFailed``
    for ``step``. ``what`` names the request. Only GitHub's ``message`` (or a text answer) goes
    into the error, never the rest of a body, and it passes through ``scrub`` first, so a token
    echoed in it is gone. ``main`` cuts the error to ``ERROR_LIMIT`` after removing the tokens the
    run holds, so the cut can never leave half of a token behind."""
    status, body = got
    lead = f"{what}: " if what else ""
    if not 200 <= status < 300:
        said = body.get("message") if isinstance(body, dict) else body
        raise StepFailed(step, status, f"{lead}{scrub(str(said))}")
    missing = [p for p in paths if dig(body, p) is None]
    if missing:
        raise StepFailed(step, status, f"{lead}the answer has no {', '.join(missing)}")
    return body


class Spike:
    def __init__(self, repo: str, apps: dict, keys: str, check_branch: str = "",
                 person_token: str = ""):
        self.repo, self.apps, self.keys = repo, apps, os.path.expanduser(keys)
        self.check_branch = check_branch
        self.person_token = person_token
        self.tokens: dict = {}
        self.slugs: dict = {}

    def secrets(self) -> list[str]:
        """The tokens this run holds (each App's installation token, the person's), for ``scrub``."""
        return [held["token"] for held in self.tokens.values()] + [self.person_token]

    # -- authentication ------------------------------------------------------------------------
    def key(self, role: str) -> str:
        found = glob.glob(os.path.join(self.keys, f"*{role}*.pem"))
        if len(found) != 1:
            raise SystemExit(f"{role}: expected one *{role}*.pem in {self.keys}, found {len(found)}")
        if os.stat(found[0]).st_mode & 0o077:
            raise SystemExit(f"{found[0]} is readable by others: chmod 600 it")
        return found[0]

    def jwt(self, role: str) -> str:
        now = int(time.time())
        signing = (b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." +
                   b64url(json.dumps({"iat": now - 60, "exp": now + 540,
                                      "iss": str(self.apps[role])}).encode()))
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key(role)],
                             input=signing.encode(), capture_output=True, check=True).stdout
        return signing + "." + b64url(sig)

    def call(self, method, path, auth, body=None):
        req = urllib.request.Request(path if path.startswith("http") else API + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("Authorization", auth)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw, status = resp.read().decode(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode(), exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw

    def app(self, role, method, path, body=None):
        return self.call(method, path, "Bearer " + self.jwt(role), body)

    def token(self, role: str) -> str:
        cached = self.tokens.get(role)
        if cached and cached["exp"] - time.time() > 300:
            return cached["token"]
        status, insts = self.app(role, "GET", "/app/installations")
        if status != 200 or not insts:
            raise SystemExit(f"{role}: no installation ({status})")
        status, got = self.app(role, "POST", f"/app/installations/{insts[0]['id']}/access_tokens",
                               {"repositories": [self.repo.split("/")[1]]})
        if status != 201:
            raise SystemExit(f"{role}: token not minted ({status})")
        self.tokens[role] = {"token": got["token"], "exp": epoch(got["expires_at"]),
                             "facts": {"length": len(got["token"]), "prefix": got["token"][:4],
                                       "expires_at": got["expires_at"],
                                       "installation_id": insts[0]["id"],
                                       "permissions": got.get("permissions")}}
        return got["token"]

    def gh(self, role, method, path, body=None):
        # "person" is not an App: its token comes from the environment, never from a JWT.
        token = self.person_token if role == "person" else self.token(role)
        return self.call(method, path, "token " + token, body)

    def graphql(self, query, variables):
        status, body = self.call("POST", API + "/graphql", "token " + self.token("reader"),
                                 {"query": query, "variables": variables})
        return (body or {}).get("data") if isinstance(body, dict) else None

    def deliveries(self, role: str, since: float, event_prefix: str = "") -> tuple[int, list | None]:
        """``(read_status, events)`` for the App's recorded webhook deliveries since ``since``.

        ``events`` is ``None`` when the list read did not answer 200: an unread list is not an
        empty one, so a failed read cannot pass for "no event was sent". Each event carries the
        ``detail_status`` of the read of its own payload; when that is not 200 its payload fields
        are ``None`` because the payload was not read, not because it had none."""
        status, items = self.app(role, "GET", "/app/hook/deliveries?per_page=100")
        if status != 200:
            return status, None
        out = []
        for item in items:
            if epoch(item["delivered_at"]) < since or not item["event"].startswith(event_prefix):
                continue
            detail_status, full = self.app(role, "GET", f"/app/hook/deliveries/{item['id']}")
            payload = ((full or {}).get("request") or {}).get("payload") or {}
            review = payload.get("review") or {}
            out.append({"event": item["event"], "action": item.get("action"),
                        "at": item["delivered_at"], "detail_status": detail_status,
                        "sender": (payload.get("sender") or {}).get("login"),
                        "sender_type": (payload.get("sender") or {}).get("type"),
                        "requested_reviewer": (payload.get("requested_reviewer") or {}).get("login"),
                        "review_state": review.get("state"),
                        "review_user": (review.get("user") or {}).get("login"),
                        "review_user_type": (review.get("user") or {}).get("type"),
                        "review_commit": (review.get("commit_id") or "")[:7],
                        "installation": bool(payload.get("installation"))})
        return status, sorted(out, key=lambda d: d["at"])

    # -- helpers on the repository --------------------------------------------------------------
    def put_file(self, role, branch, text, message):
        """Replace ``app.py`` on ``branch``; the ``(status, body)`` of the commit. A missing file
        or branch, or a refused commit, raises ``StepFailed`` for step ``put_file``."""
        f = expect("put_file", self.gh(role, "GET", f"/repos/{self.repo}/contents/app.py?ref={branch}"),
                   "sha", what=f"{role} GET app.py on {branch}")
        got = self.gh(role, "PUT", f"/repos/{self.repo}/contents/app.py",
                      {"message": message, "branch": branch, "sha": f["sha"],
                       "content": base64.b64encode(text.encode()).decode()})
        expect("put_file", got, "commit.sha", what=f"{role} PUT app.py on {branch}")
        return got

    def branch(self, role, name, base="main"):
        """Create ``name`` at the tip of ``base``; the status of the creation. A missing ``base``
        or a refused creation raises ``StepFailed`` for step ``branch``."""
        ref = expect("branch", self.gh(role, "GET", f"/repos/{self.repo}/git/ref/heads/{base}"),
                     "object.sha", what=f"{role} GET ref {base}")
        got = self.gh(role, "POST", f"/repos/{self.repo}/git/refs",
                      {"ref": f"refs/heads/{name}", "sha": ref["object"]["sha"]})
        expect("branch", got, what=f"{role} POST ref {name}")
        return got[0]

    def open_pr(self, role, title, head, base="main", body=""):
        """Open a PR from ``head`` into ``base``; the PR object. A refused request or an answer
        without a number or an author raises ``StepFailed`` for step ``open_pr``."""
        payload = {"title": title, "head": head, "base": base}
        if body:
            payload["body"] = body
        return expect("open_pr", self.gh(role, "POST", f"/repos/{self.repo}/pulls", payload),
                      "number", "user", what=f"{role} POST pulls for {head}")

    def reviews_by(self, role, number):
        """The ``[state, commit]`` of each review the ``role`` App left on the PR, oldest first."""
        _, bot = self.gh(role, "GET", f"/users/{self.slugs[role]}[bot]")
        _, items = self.gh(role, "GET", f"/repos/{self.repo}/pulls/{number}/reviews?per_page=100")
        return [[r["state"], (r.get("commit_id") or "")[:7]] for r in items or []
                if (r.get("user") or {}).get("id") == bot.get("id")]

    def settled_protection(self, number, tries=5, pause=POST_PUSH_WAIT):
        """``protection`` after waiting ``pause`` seconds, and again (at most ``tries`` times in
        all) while GitHub still answers ``mergeStateStatus`` UNKNOWN or nothing, so a late answer
        is not printed as the observation. The last answer is returned even if unsettled."""
        got = None
        for _ in range(tries):
            time.sleep(pause)
            got = self.protection(number)
            if (got or {}).get("mergeStateStatus") not in (None, "UNKNOWN"):
                break
        return got

    def protection(self, number):
        data = self.graphql("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
                            "{pullRequest(number:$n){reviewDecision mergeStateStatus}}}",
                            {"o": self.repo.split("/")[0], "r": self.repo.split("/")[1], "n": number})
        return ((data or {}).get("repository") or {}).get("pullRequest")


def emit(step, **data):
    print(json.dumps({"step": step, **data}, default=str), flush=True)


def emit_deliveries(s: Spike, step: str, role: str, since: float, key: str, prefix: str = "",
                    named: bool = False) -> None:
    """Print one delivery read with its ``read_status``; the ``key`` field is ``null`` (not an
    empty list) when the read failed, so a failed read never looks like "no events". ``named``
    adds the ``role`` field to the line."""
    status, events = s.deliveries(role, since, prefix)
    emit(step, **({"role": role} if named else {}), read_status=status, **{key: events})


def run(s: Spike) -> None:
    tag = time.strftime("%Y%m%d%H%M%S")
    t0 = time.time() - 5

    # identity: each App, its bot principal and its token (never the token itself)
    for role in ROLES:
        app = expect("identity", s.app(role, "GET", "/app"), "slug", what=f"{role} GET /app")
        s.slugs[role] = app["slug"]
        _, bot = s.gh(role, "GET", f"/users/{app['slug']}[bot]")
        emit("identity", role=role, slug=app["slug"], events=app.get("events"),
             bot_login=bot.get("login"), bot_id=bot.get("id"), bot_type=bot.get("type"),
             token=s.tokens[role]["facts"])
    reviewer_bot = f"{s.slugs['reviewer']}[bot]"

    # Q1/Q7: the fixer App opens a PR and pushes again (API commits)
    branch = f"spike-fix-{tag}"
    s.branch("fixer", branch)
    s.put_file("fixer", branch, "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
               "    return a - b\n", "Add sub")
    pr = s.open_pr("fixer", f"Spike {tag}: fixer App PR", branch, body="GitHub App feasibility spike.")
    number = pr["number"]
    _, c2 = s.put_file("fixer", branch, "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
                       "    return a - b\n\n\ndef mul(a, b):\n    return a * b\n", "Add mul")
    head = c2["commit"]["sha"]
    cm = expect("fixer_pr", s.gh("reader", "GET", f"/repos/{s.repo}/commits/{head}"),
                "commit.author", "commit.verification.verified", what=f"reader GET commit {head[:7]}")
    emit("fixer_pr", pr=number, pr_user=pr["user"]["login"], pr_user_type=pr["user"]["type"],
         commit_author=cm["commit"]["author"], author_login=(cm.get("author") or {}).get("login"),
         verified=cm["commit"]["verification"]["verified"])

    # Q3: request the reviewer bot (both login forms), then a human for contrast
    t3 = time.time() - 2
    for who in (reviewer_bot, s.slugs["reviewer"]):
        status, body = s.gh("fixer", "POST", f"/repos/{s.repo}/pulls/{number}/requested_reviewers",
                            {"reviewers": [who]})
        emit("request_reviewer", who=who, status=status,
             requested=[u["login"] for u in (body or {}).get("requested_reviewers", [])]
             if isinstance(body, dict) else body)

    # Q2/Q4: the reviewer App blocks with an inline comment, then approves, at the exact head
    t2 = time.time() - 2
    before = s.protection(number)
    status, rc = s.gh("reviewer", "POST", f"/repos/{s.repo}/pulls/{number}/reviews",
                      {"commit_id": head, "event": "REQUEST_CHANGES", "body": "F1: mul has no test.",
                       "comments": [{"path": "app.py", "line": 9, "side": "RIGHT",
                                     "body": "No test for mul."}]})
    emit("review_request_changes", status=status, state=rc.get("state"),
         commit=rc.get("commit_id", "")[:7], user=rc["user"]["login"], user_type=rc["user"]["type"])
    time.sleep(4)
    after_rc = s.protection(number)
    status, ap = s.gh("reviewer", "POST", f"/repos/{s.repo}/pulls/{number}/reviews",
                      {"commit_id": head, "event": "APPROVE", "body": "F1: fixed."})
    time.sleep(6)
    emit("review_approve", status=status, state=ap.get("state"), commit=ap.get("commit_id", "")[:7],
         protection_before=before, protection_after_request_changes=after_rc,
         protection_after_approve=s.protection(number))

    # Q7: the fixer pushes with git over HTTPS; the token reaches git through GIT_ASKPASS only
    with tempfile.TemporaryDirectory() as tmp:
        askpass = os.path.join(tmp, "askpass.sh")
        with open(askpass, "w") as fh:
            fh.write('#!/bin/sh\ncase "$1" in Username*) echo x-access-token ;; '
                     '*) echo "$SPIKE_TOKEN" ;; esac\n')
        os.chmod(askpass, 0o700)
        env = {**os.environ, "SPIKE_TOKEN": s.token("fixer"), "GIT_ASKPASS": askpass,
               "GIT_TERMINAL_PROMPT": "0"}
        clone = os.path.join(tmp, "clone")
        git = lambda *a, cwd=clone: subprocess.run(["git", *a], env=env, cwd=cwd,  # noqa: E731
                                                   capture_output=True, text=True)
        git("clone", "-q", "--branch", branch, f"https://github.com/{s.repo}.git", clone, cwd=tmp)
        bot = expect("git_push", s.gh("fixer", "GET", f"/users/{s.slugs['fixer']}[bot]"), "id",
                     what="fixer GET its bot user")
        git("config", "user.name", f"{s.slugs['fixer']}[bot]")
        git("config", "user.email", f"{bot['id']}+{s.slugs['fixer']}[bot]@users.noreply.github.com")
        with open(os.path.join(clone, "app.py"), "a") as fh:
            fh.write("\n\ndef div(a, b):\n    return a / b\n")
        git("commit", "-qam", "Add div (git push)")
        pushed = git("push", "-q", "origin", branch)
        sha = git("rev-parse", "HEAD").stdout.strip()
    settled = s.settled_protection(number)
    cm = expect("git_push", s.gh("reader", "GET", f"/repos/{s.repo}/commits/{sha}"),
                "commit.verification.verified", "commit.verification.reason",
                what=f"reader GET commit {sha[:7]}")
    emit("git_push", returncode=pushed.returncode, author_login=(cm.get("author") or {}).get("login"),
         verified=cm["commit"]["verification"]["verified"],
         reason=cm["commit"]["verification"]["reason"], new_head=sha[:7],
         reviews_after_push=s.reviews_by("reviewer", number),
         protection_after_push=settled)

    # Tuck's point: what the reviewer's pull_requests:write allows beyond reviews
    emit("reviewer_permissions", **reviewer_probes(s, branch, number, tag))
    stale_approval(s, tag)

    # Q1/Q2/Q3/Q5: which events each App's webhook recorded
    time.sleep(15)
    for role in ROLES:
        emit_deliveries(s, "deliveries", role, t0, "since_start", named=True)
    emit_deliveries(s, "deliveries_after_reviewer_requests", "reader", t3, "reader", "pull_request")
    emit_deliveries(s, "review_deliveries", "reader", t2, "reader", "pull_request_review")
    if s.check_branch:
        required_check(s, tag)


def reviewer_probes(s: Spike, branch: str, number: int, tag: str) -> dict:
    """The status of each thing the reviewer App's ``pull_requests: write`` might allow beyond
    reviews. Closing comes last and is followed by a reopen, so the PR is left open."""
    f = expect("reviewer_permissions",
               s.gh("reviewer", "GET", f"/repos/{s.repo}/contents/app.py?ref={branch}"), "sha",
               what=f"reviewer GET app.py on {branch}")
    pr = f"/repos/{s.repo}/pulls/{number}"
    probes = {}
    probes["push_contents"] = s.gh("reviewer", "PUT", f"/repos/{s.repo}/contents/app.py",
                                   {"message": "x", "branch": branch, "sha": f["sha"],
                                    "content": base64.b64encode(b"x\n").decode()})[0]
    probes["merge"] = s.gh("reviewer", "PUT", pr + "/merge", {})[0]
    probes["edit_title"] = s.gh("reviewer", "PATCH", pr,
                                {"title": f"Spike {tag}: edited by the reviewer App"})[0]
    probes["add_labels"] = s.gh("reviewer", "POST", f"/repos/{s.repo}/issues/{number}/labels",
                                {"labels": ["spike"]})[0]
    probes["close_pr"] = s.gh("reviewer", "PATCH", pr, {"state": "closed"})[0]
    probes["reopen_pr"] = s.gh("reviewer", "PATCH", pr, {"state": "open"})[0]
    return probes


def stale_approval(s: Spike, tag: str) -> None:
    """Q6: the reviewer App approves a person's PR, then the person pushes. Does GitHub dismiss
    the approval (``main`` dismisses stale approvals)? Needs the person's token in
    ``s.person_token``; without it the step is reported as skipped."""
    if not s.person_token:
        emit("stale_approval", skipped="no person token (see --person-token-env)")
        return
    branch = f"spike-person-{tag}"
    s.branch("person", branch)
    _, c1 = s.put_file("person", branch, "def add(a, b):\n    return a + b\n\n\n"
                       "def neg(a):\n    return -a\n", "Add neg")
    pr = s.open_pr("person", f"Spike {tag}: person PR", branch)
    number, head = pr["number"], c1["commit"]["sha"]
    status, ap = s.gh("reviewer", "POST", f"/repos/{s.repo}/pulls/{number}/reviews",
                      {"commit_id": head, "event": "APPROVE", "body": "Fine."})
    time.sleep(6)
    before = s.protection(number)
    _, c2 = s.put_file("person", branch, "def add(a, b):\n    return a + b\n\n\n"
                       "def neg(a):\n    return -a\n\n\ndef inv(a):\n    return 1 / a\n",
                       "Add inv")
    after_push = s.settled_protection(number)
    emit("stale_approval", pr=number, pr_user_type=pr["user"]["type"], approve_status=status,
         approve_state=ap.get("state"), approved_commit=head[:7], new_head=c2["commit"]["sha"][:7],
         protection_after_approve=before, reviews_after_push=s.reviews_by("reviewer", number),
         protection_after_push=after_push)


def required_check(s: Spike, tag: str) -> None:
    """Q9: a check run posted by the reviewer App as the merge gate. ``s.check_branch`` must
    require the check ``diaktoros/review`` pinned to the reviewer App's id and no approvals, and
    the reviewer App needs ``Checks: write``."""
    def merge_state(number):
        # GitHub answers UNKNOWN while it recomputes after a change: wait for a settled state.
        return (s.settled_protection(number, tries=10) or {}).get("mergeStateStatus")

    branch = f"spike-check-{tag}"
    s.branch("fixer", branch, base=s.check_branch)
    _, c = s.put_file("fixer", branch, f"def add(a, b):\n    return a + b  # {tag}\n", "Check test")
    head = c["commit"]["sha"]
    pr = s.open_pr("fixer", f"Spike {tag}: required check", branch, base=s.check_branch)
    number, states = pr["number"], {}
    states["no_check"] = merge_state(number)
    status, run = s.gh("reviewer", "POST", f"/repos/{s.repo}/check-runs",
                       {"name": "diaktoros/review", "head_sha": head, "status": "in_progress",
                        "output": {"title": "Review running", "summary": "Reviewing this head."}})
    states["create_status"] = status
    states["pending"] = merge_state(number)
    s.gh("reviewer", "PATCH", f"/repos/{s.repo}/check-runs/{run['id']}",
         {"status": "completed", "conclusion": "failure",
          "output": {"title": "Changes requested", "summary": "A finding blocks."}})
    states["failure"] = merge_state(number)
    states["fixer_creates_check"] = s.gh("fixer", "POST", f"/repos/{s.repo}/check-runs",
                                         {"name": "diaktoros/review", "head_sha": head,
                                          "status": "completed", "conclusion": "success"})[0]
    states["after_fixer_attempt"] = merge_state(number)
    s.gh("reviewer", "PATCH", f"/repos/{s.repo}/check-runs/{run['id']}",
         {"conclusion": "success", "output": {"title": "Approved", "summary": "At this head."}})
    states["success"] = merge_state(number)
    s.put_file("fixer", branch, f"def add(a, b):\n    return a + b  # {tag} v2\n", "New head")
    time.sleep(10)
    states["after_new_push"] = merge_state(number)
    emit("required_check", pr=number, **states)


def app_arg(text: str) -> tuple[str, int]:
    """Parse one ``--app ROLE=ID`` value. A value without ``=``, or whose ID is not a number, is
    an ``ArgumentTypeError``, which argparse prints as a usage error rather than a traceback."""
    role, sep, app_id = text.partition("=")
    if not sep or not role or not app_id.isascii() or not app_id.isdigit():
        raise argparse.ArgumentTypeError(f"{text!r} is not ROLE=ID (for example reader=12345)")
    return role, int(app_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="the throwaway OWNER/REPO")
    ap.add_argument("--app", action="append", default=[], metavar="ROLE=ID", type=app_arg,
                    help="reader=ID, reviewer=ID and fixer=ID")
    ap.add_argument("--keys", required=True, help="directory holding one *ROLE*.pem per App")
    ap.add_argument("--check-branch", default="",
                    help="a base branch requiring the check diaktoros/review pinned to the "
                         "reviewer App, with no required approvals: runs step Q9")
    ap.add_argument("--person-token-env", default=PERSON_TOKEN_ENV, metavar="NAME",
                    help="the environment variable holding a person's token, for step Q6")
    args = ap.parse_args(argv)
    apps = dict(args.app)
    if set(apps) != set(ROLES):
        ap.error("give --app for exactly reader, reviewer and fixer")
    harness = Spike(args.repo, apps, args.keys, args.check_branch,
                    os.environ.get(args.person_token_env, ""))
    try:
        run(harness)
    except StepFailed as exc:
        # ``expect`` removed what is shaped like a token; this removes the tokens this run holds
        # (a legacy personal token has no prefix to recognise), and only then cuts.
        emit(exc.step, status=exc.status, error=scrub(exc.error, harness.secrets())[:ERROR_LIMIT])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
