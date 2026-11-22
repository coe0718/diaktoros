#!/usr/bin/env python3
"""Refresh docs/site/data.json from the GitHub API.

The page prefers live API data in the browser; this script refreshes the
checked-in snapshot used as the offline/API-outage fallback.

The Pages deploy workflow (.github/workflows/deploy-site.yml) runs this
before publishing, on every push to main and every published release, so
the launch page's statistics (release, stars, PR/issue counts, commit
activity, last push) are read from the repository at build time instead of
typed by hand. The refreshed file goes into the deployed artifact only;
nothing is committed. Safe to run locally; on API failure the previous
snapshot is kept: the newer of the checked-in file and, given
``--previous``, the data.json the site last published.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

OWNER = "coe0718"
REPO = "diaktoros"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data.json")
API = "https://api.github.com"
# stats/commit_activity answers an empty body (HTTP 202) while GitHub
# computes it. Ask again a few times before retaining the previous count,
# because retaining it also holds back the snapshot date (#1773).
ACTIVITY_ATTEMPTS = 4
ACTIVITY_WAIT = 5  # seconds between attempts


def api(path):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "diaktoros-site-data"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _util():
    """The plugin's helpers (standard library only), from the checkout this runs in."""
    root = os.path.dirname(os.path.dirname(HERE))
    if root not in sys.path:
        sys.path.insert(0, root)
    from diaktoros import util
    return util


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


def commit_activity():
    """Weekly commit activity, asking again while GitHub still computes it.

    Returns the list of weeks, or an empty list when every attempt came
    back empty (the caller then keeps the previous count and date). An
    error on the last attempt propagates to the caller.
    """
    path = "/repos/%s/%s/stats/commit_activity" % (OWNER, REPO)
    for attempt in range(ACTIVITY_ATTEMPTS):
        if attempt:
            time.sleep(ACTIVITY_WAIT)
        try:
            weeks = api(path)
        except Exception:
            if attempt == ACTIVITY_ATTEMPTS - 1:
                raise
            continue
        if weeks:
            return weeks
    return []


def _load(path):
    """A snapshot as a dict, or None when it is missing or not a JSON object."""
    try:
        with open(path) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    return snap if isinstance(snap, dict) else None


def _pushed(snap):
    return _util().iso_epoch(snap.get("pushed_at"))


def base_snapshot(previous=None):
    """The snapshot a refresh starts from, and keeps when the API fails.

    The deploy refreshes into the artifact only, so the checked-in file can
    be older than what the site last published. ``previous`` (the deployed
    data.json, when the workflow could fetch it) wins when its last push is
    later than the checked-in file's: an outage then keeps the newest
    published figures rather than rolling them back (Vex, PR #2570 r1).
    """
    checked_in = _load(OUT) or {}
    deployed = _load(previous) if previous else None
    if deployed and _pushed(deployed) and (
            not _pushed(checked_in) or _pushed(deployed) > _pushed(checked_in)):
        return deployed
    return checked_in


def refresh(data):
    """Refresh ``data`` in place from the API; any failed read raises."""
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
        weeks = commit_activity()
        # Still empty after every retry: a zero there would
        # overwrite a real snapshot with nothing.
        activity_ok = False
        if weeks:
            data["commits_year"] = sum(w["total"] for w in weeks)
            activity_ok = True
    except Exception:
        activity_ok = False
        pass  # endpoint computes async; keep the previous value

    # Derived, same formulas as the page's derive().
    if data.get("prs_merged"):
        data["loop_share"] = "%d%%" % round(
            100 * data["loop_prs_merged"] / data["prs_merged"])
    if data.get("open_issues_count") is not None and data.get("prs_open") is not None:
        data["issues_open"] = data["open_issues_count"] - data["prs_open"]
    data["last_activity"] = _rel_time(data["pushed_at"])

    # The stamp dates the whole snapshot: only advance it when the
    # commit count was actually refreshed, or a retained count would
    # wear a newer date than its own (Vex, PR #1750 rounds 2-3). A
    # retained count with no earlier date stays undated: stamping
    # it today would claim an age nobody knows. commit_activity()
    # asks the still-computing endpoint again first, so the date is
    # held back only when every attempt this run came back empty,
    # the last attempt raised (an empty HTTP 202 body fails to parse
    # as JSON in api(), so it raises rather than reading empty), or
    # the weeks it returned could not be summed (#1773, #2438).
    if activity_ok:
        data["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data.pop("error", None)


def _write(data):
    """Whole or not at all, through the repo's one publishing helper (#2159)."""
    _util().atomic_write(OUT, json.dumps(data, indent=2) + "\n", mode=0o644)


def main(argv=None):
    """``build-data.py [--previous DEPLOYED_DATA_JSON]``"""
    argv = list(argv or [])
    previous = argv[1] if argv[:1] == ["--previous"] and len(argv) == 2 else None
    if argv and previous is None:
        print("usage: build-data.py [--previous DEPLOYED_DATA_JSON]", file=sys.stderr)
        return 2
    prev = base_snapshot(previous)
    # Written first, so a step killed mid-refresh (the deploy bounds it)
    # still publishes the newest snapshot rather than the checked-in one.
    _write(prev)
    # All or nothing: the refresh works on a copy, so a read that fails
    # half-way never leaves new figures beside old ones (Vex, PR #2570 r1).
    staged = dict(prev)
    try:
        refresh(staged)
        data = staged
    except Exception as exc:  # keep the previous snapshot on failure
        data = dict(prev)
        data["error"] = "%s: %s" % (type(exc).__name__, exc)
        print("warning: %s; kept previous data.json" % data["error"], file=sys.stderr)

    # Curated, not API-derived.
    data["demo_pr"] = {
        "number": 530,
        "url": "https://github.com/%s/%s/pull/530" % (OWNER, REPO),
        "title": "Add fix_ci to every settings path",
    }
    _write(data)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
