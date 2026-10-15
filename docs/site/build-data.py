#!/usr/bin/env python3
"""Refresh docs/site/data.json from the GitHub API.

The page prefers live API data in the browser; this script refreshes the
checked-in snapshot used as the offline/API-outage fallback.

The Pages deploy workflow runs this before publishing so the launch page's
statistics (release, stars, PR/issue counts, commit activity, last push)
are read from the repository at build time instead of typed by hand. Safe to run locally; on API failure
the previous snapshot is kept.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

OWNER = "coe0718"
REPO = "diaktoros"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data.json")
API = "https://api.github.com"


def api(path):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "diaktoros-site-data"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _rel_time(iso):
    """Human 'xh ago' for an ISO-8601 timestamp; mirrors the page's relTime."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return "%dm ago" % mins
    hours = mins // 60
    if hours < 24:
        return "%dh ago" % hours
    days = hours // 24
    if days < 30:
        return "%dd ago" % days
    months = days // 30
    if months < 12:
        return "%dmo ago" % months
    return "%dy ago" % (months // 12)


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            prev = json.load(f)
    data = dict(prev)
    try:
        repo = api("/repos/%s/%s" % (OWNER, REPO))
        data["stars"] = repo["stargazers_count"]
        data["forks"] = repo["forks_count"]
        data["open_issues_count"] = repo["open_issues_count"]
        data["pushed_at"] = repo["pushed_at"]

        rel = api("/repos/%s/%s/releases/latest" % (OWNER, REPO))
        data["version"] = rel["tag_name"]
        data["version_url"] = rel["html_url"]
        data["released"] = rel["published_at"][:10]

        def count(query):
            return api("/search/issues?q=%s&per_page=1" % query)["total_count"]

        data["prs_merged"] = count("repo:%s/%s+type:pr+is:merged" % (OWNER, REPO))
        data["loop_prs_merged"] = count(
            "repo:%s/%s+type:pr+is:merged+author:drey-coe1" % (OWNER, REPO))
        data["issues_closed"] = count("repo:%s/%s+type:issue+is:closed" % (OWNER, REPO))
        data["prs_open"] = count("repo:%s/%s+type:pr+is:open" % (OWNER, REPO))

        try:
            weeks = api("/repos/%s/%s/stats/commit_activity" % (OWNER, REPO))
            data["commits_year"] = sum(w["total"] for w in weeks)
        except Exception:
            pass  # endpoint computes async; keep the previous value

        # Derived, same formulas as the page's derive().
        if data.get("prs_merged"):
            data["loop_share"] = "%d%%" % round(
                100 * data["loop_prs_merged"] / data["prs_merged"])
        if data.get("open_issues_count") is not None and data.get("prs_open") is not None:
            data["issues_open"] = data["open_issues_count"] - data["prs_open"]
        data["last_activity"] = _rel_time(data["pushed_at"])

        data["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data.pop("error", None)
    except Exception as exc:  # keep the previous snapshot on failure
        data["error"] = "%s: %s" % (type(exc).__name__, exc)
        print("warning: %s; kept previous data.json" % data["error"], file=sys.stderr)

    # Curated, not API-derived.
    data["demo_pr"] = {
        "number": 530,
        "url": "https://github.com/%s/%s/pull/530" % (OWNER, REPO),
        "title": "Add fix_ci to every settings path",
    }
    with open(OUT, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
