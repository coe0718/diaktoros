#!/usr/bin/env python3
"""Delete old GitHub Actions runs, keeping the newest N and every run the loop still reads (#902).

    python .github/scripts/prune_runs.py --keep 100 [--dry-run]

Runs in the ``prune-runs`` workflow with its own ``GITHUB_TOKEN`` (``GITHUB_REPOSITORY`` names the
repository). Deleting a run removes its logs and artifacts with it.

Never deleted, whatever their age:
- a run whose commit is an open PR's head or the default branch's head: the loop reads those runs'
  checks (the required-checks gate, #368) and job logs (per-test evidence, #687), and a pruned one
  would hold that PR's review as "required checks never reported";
- a run that is not finished (queued, waiting, in progress).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
PAGES_MAX = 100                       # 100 runs a page: at most 10,000 runs read per prune
# The workflow token allows about 1,000 API requests an hour: a backlog drains over several runs.
DELETE_MAX = 400


def plan(runs: list[dict], protected: set[str], keep: int) -> list[dict]:
    """The runs to delete: all but the newest ``keep``, never a protected head's run, never an
    unfinished one. ``runs`` are GitHub's run objects (``id``, ``created_at``, ``head_sha``,
    ``status``)."""
    if keep < 0:
        raise ValueError("keep must be 0 or more")
    newest = sorted(runs, key=lambda run: (str(run.get("created_at") or ""), run.get("id") or 0),
                    reverse=True)
    return [run for run in newest[keep:]
            if run.get("status") == "completed" and run.get("head_sha") not in protected]


def _call(method: str, path: str, token: str):
    request = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diaktoros-prune-runs"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def _pages(path: str, token: str, key: str | None = None) -> list[dict]:
    out = []
    for page in range(1, PAGES_MAX + 1):
        sep = "&" if "?" in path else "?"
        got = _call("GET", f"{path}{sep}per_page=100&page={page}", token)
        items = got.get(key, []) if key else got
        out += items
        if len(items) < 100:
            return out
    raise RuntimeError(f"more than {PAGES_MAX} pages at {path}: not pruning a partial list")


def protected_heads(repo: str, token: str) -> set[str]:
    heads = {str((pr.get("head") or {}).get("sha") or "")
             for pr in _pages(f"/repos/{repo}/pulls?state=open", token)}
    info = _call("GET", f"/repos/{repo}", token)
    branch = _call("GET", f"/repos/{repo}/branches/{info['default_branch']}", token)
    heads.add(str((branch.get("commit") or {}).get("sha") or ""))
    heads.discard("")
    return heads


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-delete", type=int, default=DELETE_MAX,
                        help="most runs deleted in one prune (the rest wait for the next)")
    args = parser.parse_args(argv)
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    # The protected heads first: a run created after this read is newer than every kept run.
    protected = protected_heads(repo, token)
    runs = _pages(f"/repos/{repo}/actions/runs", token, key="workflow_runs")
    doomed = plan(runs, protected, args.keep)
    print(f"{len(runs)} runs; keeping the newest {args.keep}, the runs of {len(protected)} "
          f"protected head(s) and every unfinished run; {len(doomed)} to delete")
    if args.dry_run:
        for run in doomed[:20]:
            print(f"  would delete {run['id']} {run.get('name')} {run.get('created_at')}")
        return 0
    # Oldest first, so a capped or rate-limited prune removes the oldest and the rest wait.
    batch = list(reversed(doomed))[:max(0, args.max_delete)]
    deleted = failed = 0
    for run in batch:
        try:
            _call("DELETE", f"/repos/{repo}/actions/runs/{run['id']}", token)
            deleted += 1
        except urllib.error.HTTPError as exc:
            exc.close()                  # it holds the error response's open body
            if exc.code in (403, 429):
                print(f"  rate limited after {deleted} deletions (HTTP {exc.code}): the rest "
                      "wait for the next prune", file=sys.stderr)
                break
            failed += 1
            print(f"  could not delete run {run['id']}: HTTP {exc.code}", file=sys.stderr)
    print(f"deleted {deleted}, failed {failed}, {len(doomed) - deleted - failed} left for the "
          "next prune")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
