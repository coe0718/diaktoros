#!/usr/bin/env python3
"""GitHub App feasibility spike (#1164): what three App identities can actually do on GitHub.

Run against a throwaway repository only. It needs three GitHub Apps (reader, reviewer, fixer),
each installed on that repository alone, and their private keys in 0600 files. It opens PRs,
pushes, reviews and requests reviewers there, then prints one JSON line per step. The results
are written up in docs/github-app-support.md.

    python3 tools/github_app_spike.py --repo OWNER/REPO \\
        --app reader=ID --app reviewer=ID --app fixer=ID --keys ~/.hermes/dk-spike

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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"
ROLES = ("reader", "reviewer", "fixer")


def epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Spike:
    def __init__(self, repo: str, apps: dict, keys: str):
        self.repo, self.apps, self.keys = repo, apps, os.path.expanduser(keys)
        self.tokens: dict = {}
        self.slugs: dict = {}

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
        return self.call(method, path, "token " + self.token(role), body)

    def graphql(self, query, variables):
        status, body = self.call("POST", API + "/graphql", "token " + self.token("reader"),
                                 {"query": query, "variables": variables})
        return (body or {}).get("data") if isinstance(body, dict) else None

    def deliveries(self, role: str, since: float, event_prefix: str = "") -> list[dict]:
        status, items = self.app(role, "GET", "/app/hook/deliveries?per_page=100")
        if status != 200:
            return []
        out = []
        for item in items:
            if epoch(item["delivered_at"]) < since or not item["event"].startswith(event_prefix):
                continue
            _, full = self.app(role, "GET", f"/app/hook/deliveries/{item['id']}")
            payload = ((full or {}).get("request") or {}).get("payload") or {}
            review = payload.get("review") or {}
            out.append({"event": item["event"], "action": item.get("action"),
                        "at": item["delivered_at"],
                        "sender": (payload.get("sender") or {}).get("login"),
                        "sender_type": (payload.get("sender") or {}).get("type"),
                        "requested_reviewer": (payload.get("requested_reviewer") or {}).get("login"),
                        "review_state": review.get("state"),
                        "review_user": (review.get("user") or {}).get("login"),
                        "review_user_type": (review.get("user") or {}).get("type"),
                        "review_commit": (review.get("commit_id") or "")[:7],
                        "installation": bool(payload.get("installation"))})
        return sorted(out, key=lambda d: d["at"])

    # -- helpers on the repository --------------------------------------------------------------
    def put_file(self, role, branch, text, message):
        _, f = self.gh(role, "GET", f"/repos/{self.repo}/contents/app.py?ref={branch}")
        return self.gh(role, "PUT", f"/repos/{self.repo}/contents/app.py",
                       {"message": message, "branch": branch, "sha": f["sha"],
                        "content": base64.b64encode(text.encode()).decode()})

    def branch(self, role, name):
        _, ref = self.gh(role, "GET", f"/repos/{self.repo}/git/ref/heads/main")
        return self.gh(role, "POST", f"/repos/{self.repo}/git/refs",
                       {"ref": f"refs/heads/{name}", "sha": ref["object"]["sha"]})[0]

    def protection(self, number):
        data = self.graphql("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
                            "{pullRequest(number:$n){reviewDecision mergeStateStatus}}}",
                            {"o": self.repo.split("/")[0], "r": self.repo.split("/")[1], "n": number})
        return ((data or {}).get("repository") or {}).get("pullRequest")


def emit(step, **data):
    print(json.dumps({"step": step, **data}, default=str), flush=True)


def run(s: Spike) -> None:
    tag = time.strftime("%Y%m%d%H%M%S")
    t0 = time.time() - 5

    # identity: each App, its bot principal and its token (never the token itself)
    for role in ROLES:
        _, app = s.app(role, "GET", "/app")
        s.slugs[role] = app["slug"]
        _, bot = s.gh(role, "GET", f"/users/{app['slug']}[bot]")
        emit("identity", role=role, slug=app["slug"], events=app.get("events"),
             bot_login=bot.get("login"), bot_id=bot.get("id"), bot_type=bot.get("type"),
             token=s.tokens[role]["facts"])
    reviewer_bot = f"{s.slugs['reviewer']}[bot]"

    # Q1/Q7: the fixer App opens a PR and pushes again (API commits)
    branch = f"spike-fix-{tag}"
    s.branch("fixer", branch)
    _, c1 = s.put_file("fixer", branch, "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
                       "    return a - b\n", "Add sub")
    _, pr = s.gh("fixer", "POST", f"/repos/{s.repo}/pulls",
                 {"title": f"Spike {tag}: fixer App PR", "head": branch, "base": "main",
                  "body": "GitHub App feasibility spike."})
    number = pr["number"]
    _, c2 = s.put_file("fixer", branch, "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
                       "    return a - b\n\n\ndef mul(a, b):\n    return a * b\n", "Add mul")
    head = c2["commit"]["sha"]
    _, cm = s.gh("reader", "GET", f"/repos/{s.repo}/commits/{head}")
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
        _, bot = s.gh("fixer", "GET", f"/users/{s.slugs['fixer']}[bot]")
        git("config", "user.name", f"{s.slugs['fixer']}[bot]")
        git("config", "user.email", f"{bot['id']}+{s.slugs['fixer']}[bot]@users.noreply.github.com")
        with open(os.path.join(clone, "app.py"), "a") as fh:
            fh.write("\n\ndef div(a, b):\n    return a / b\n")
        git("commit", "-qam", "Add div (git push)")
        pushed = git("push", "-q", "origin", branch)
        sha = git("rev-parse", "HEAD").stdout.strip()
    time.sleep(3)
    _, cm = s.gh("reader", "GET", f"/repos/{s.repo}/commits/{sha}")
    emit("git_push", returncode=pushed.returncode, author_login=(cm.get("author") or {}).get("login"),
         verified=cm["commit"]["verification"]["verified"],
         reason=cm["commit"]["verification"]["reason"])

    # Tuck's point: what the reviewer's pull_requests:write allows beyond reviews
    _, f = s.gh("reviewer", "GET", f"/repos/{s.repo}/contents/app.py?ref={branch}")
    probes = {
        "push_contents": s.gh("reviewer", "PUT", f"/repos/{s.repo}/contents/app.py",
                              {"message": "x", "branch": branch, "sha": f["sha"],
                               "content": base64.b64encode(b"x\n").decode()})[0],
        "merge": s.gh("reviewer", "PUT", f"/repos/{s.repo}/pulls/{number}/merge", {})[0],
        "edit_title": s.gh("reviewer", "PATCH", f"/repos/{s.repo}/pulls/{number}",
                           {"title": f"Spike {tag}: edited by the reviewer App"})[0],
        "close_pr": s.gh("reviewer", "PATCH", f"/repos/{s.repo}/pulls/{number}",
                         {"state": "closed"})[0],
    }
    emit("reviewer_permissions", **probes)

    # Q1/Q2/Q3/Q5: which events each App's webhook recorded
    time.sleep(15)
    for role in ROLES:
        emit("deliveries", role=role, since_start=s.deliveries(role, t0))
    emit("deliveries_after_reviewer_requests", reader=s.deliveries("reader", t3, "pull_request"))
    emit("review_deliveries", reader=s.deliveries("reader", t2, "pull_request_review"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="the throwaway OWNER/REPO")
    ap.add_argument("--app", action="append", default=[], metavar="ROLE=ID",
                    help="reader=ID, reviewer=ID and fixer=ID")
    ap.add_argument("--keys", required=True, help="directory holding one *ROLE*.pem per App")
    args = ap.parse_args(argv)
    apps = dict(item.split("=", 1) for item in args.app)
    if set(apps) != set(ROLES):
        ap.error("give --app for exactly reader, reviewer and fixer")
    run(Spike(args.repo, {k: int(v) for k, v in apps.items()}, args.keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
