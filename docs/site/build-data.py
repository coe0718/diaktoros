#!/usr/bin/env python3
"""Refresh docs/site/data.json from the GitHub API.

The Pages deploy workflow runs this before publishing so the launch page's
statistics (release, merged PRs, closed issues) are read from the repository
at build time instead of typed by hand. Safe to run locally; on API failure
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


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            prev = json.load(f)
    data = dict(prev)
    try:
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
