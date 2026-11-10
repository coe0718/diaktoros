"""``hermes dk stats --public --json``: what the loop's checks caught, safe to publish (#2253).

A versioned aggregate over a window, for the site's "What the second opinion caught" section
(``docs/site/publish-stats.py`` publishes it). Built from the run ledger, read-only, plus one
listing of the repository's closed issues and PRs, read as the reader. It holds counts, dates,
durations and public PR numbers only, under a fixed set of keys: never a title, body, turn output,
token count, cost, login, local path or a seat account's name. The seats are named for the job
they do: coder, reviewer, shadow and arbiter.

- **stopped**: heads the reviewer approved, the shadow asked for changes on, and the arbiter's
  ruling over that shadow upheld (REJECT). A person merges, so this is "stopped before merge",
  never "would have merged".
- **overruled**: rulings over a shadow that went the reviewer's way (ACCEPT), shown beside it.
  A ruling with no shadow (the round cap's) is neither.
- **findings**: issues filed by the reviewer, the shadow and the arbiter in the window, and how
  many of each GitHub shows closed as completed.
- **cycle**: review rounds per merged PR the loop reviewed, open-to-merge time (median and p75)
  and how many change proofs ran (#1782).
- **models** (``--models`` only): per reviewer model, see ``stats_public_models``.
"""
from __future__ import annotations

import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .ledger import read_only
from .util import epoch

SCHEMA = 1
ISSUE_PAGES = 30
_VERDICT = {"APPROVE": "approved", "APPROVED": "approved", "REQUEST_CHANGES": "changes",
            "CHANGES_REQUESTED": "changes"}
FILERS = ("reviewer", "shadow", "arbiter")


def day(epoch) -> str | None:
    """``YYYY-MM-DD`` in UTC, or None."""
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def spread(values: list[float]) -> dict | None:
    """``{n, median, p75}`` of ``values``, or None when there are none."""
    if not values:
        return None
    ordered = sorted(values)

    def at(q: float) -> float:
        pos = q * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
    return {"n": len(ordered), "median": at(0.5), "p75": at(0.75)}


def _rows(con, sql: str, args: tuple) -> list:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:          # an older ledger without that table
        return []


def read_ledger(db: Path, repo: str) -> dict:
    """The rows the export counts from, every one keyed by public numbers only; all empty
    when there is no ledger yet."""
    if not Path(db).exists():
        return {key: [] for key in ("runs", "reviews", "shadows", "rulings", "filed", "proofs")}
    con = read_only(db)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        model = "r.model" if "model" in columns else "NULL"
        return {
            "runs": _rows(con, "SELECT pr, seat, created FROM runs WHERE repo=?", (repo,)),
            "reviews": _rows(con, f"SELECT r.pr, r.head, {model} AS model, x.verdict, x.created "
                                  "FROM runs r JOIN review_receipts x ON x.run_id=r.id "
                                  "WHERE r.repo=? AND r.seat='reviewer' "
                                  "AND x.state IN ('posted','confirmed') ORDER BY x.created",
                             (repo,)),
            "shadows": _rows(con, "SELECT run_id, pr, head, verdict, created FROM shadow_reviews "
                                  "WHERE repo=?", (repo,)),
            "rulings": _rows(con, "SELECT pr, head, verdict, shadow_run, created FROM rulings "
                                  "WHERE repo=? ORDER BY created", (repo,)),
            "filed": _rows(con, "SELECT 'reviewer' AS seat, issue_number, created FROM "
                                "filed_issues WHERE repo=? AND state='posted' "
                                "AND issue_number IS NOT NULL", (repo,))
            + _rows(con, "SELECT r.seat, i.issue_number, i.created FROM shadow_issues i "
                         "JOIN runs r ON r.id=i.run_id WHERE i.repo=? "
                         "AND r.seat IN ('shadow','adjudicator') "
                         "AND i.state IN ('posted','unlabelled') "
                         "AND i.issue_number IS NOT NULL", (repo,)),
            "proofs": _rows(con, "SELECT created FROM change_proofs WHERE repo=?", (repo,))}
    finally:
        con.close()


def approvals(rows: dict) -> dict:
    """The reviewer's latest posted verdict at each ``(pr, head)``, with its model."""
    latest = {}
    for row in rows["reviews"]:                     # in creation order: the latest wins
        latest[(row["pr"], row["head"])] = (_VERDICT.get(str(row["verdict"]), "other"),
                                            row["model"] or "unknown")
    return latest


def over_shadow(rows: dict, since: float, until: float) -> list[tuple]:
    """Each ruling in the window that was shown a shadow review: ``(ruling, shadow)``; the
    shadow is None when its row is gone. A ruling with no shadow (``''`` or NULL) is left out."""
    shadows = {row["run_id"]: row for row in rows["shadows"]}
    return [(r, shadows.get(r["shadow_run"])) for r in rows["rulings"]
            if r["shadow_run"] and since <= r["created"] < until]


def caught(rows: dict, since: float, until: float) -> tuple[dict, dict]:
    """``(stopped, overruled)``: see the module doc."""
    latest = approvals(rows)
    stopped, overruled = {}, 0
    for ruling, shadow in over_shadow(rows, since, until):
        if ruling["verdict"] == "ACCEPT":
            overruled += 1
        elif (ruling["verdict"] == "REJECT" and shadow is not None
              and _VERDICT.get(str(shadow["verdict"])) == "changes"
              and (latest.get((shadow["pr"], shadow["head"])) or ("",))[0] == "approved"):
            stopped[(shadow["pr"], shadow["head"])] = int(shadow["pr"])
    prs = sorted(set(stopped.values()), reverse=True)
    return {"count": len(stopped), "prs": prs}, {"count": overruled}


def closed_listing(loop: dict, since: float) -> tuple[dict | None, bool]:
    """One listing of the repository's closed issues and PRs since ``since``, as the reader:
    ``({number: {reason, created, merged}}, partial)``; ``(None, True)`` when it failed."""
    from . import gh
    path = f"/repos/{loop['repo']}/issues?state=closed&per_page=100"
    if since > 0:
        path += "&since=" + datetime.fromtimestamp(since, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    items, reason = gh._read_pages(loop, path, "issue", ISSUE_PAGES,
                                   login=loop.get("read_token"), keep_partial=True)
    if items is None:
        return None, True
    out = {}
    for item in items:
        number = item.get("number")
        if type(number) is not int:
            continue
        pull = item.get("pull_request") if isinstance(item.get("pull_request"), dict) else None
        out[number] = {"reason": str(item.get("state_reason") or ""),
                       "created": epoch(item.get("created_at")),
                       "merged": epoch(pull.get("merged_at")) if pull else None}
    return out, bool(reason)


def findings(rows: dict, closed: dict | None, since: float, until: float) -> dict:
    """Issues each seat filed in the window and how many GitHub shows closed as completed
    (``completed`` is None when the listing could not be read)."""
    filed, done = Counter(), Counter()
    for row in rows["filed"]:
        if not since <= row["created"] < until:
            continue
        who = "arbiter" if row["seat"] == "adjudicator" else row["seat"]
        filed[who] += 1
        if closed is not None and (closed.get(row["issue_number"]) or {}).get(
                "reason") == "completed":
            done[who] += 1
    return {who: {"filed": filed[who], "completed": None if closed is None else done[who]}
            for who in FILERS}


def cycle(rows: dict, closed: dict | None, since: float, until: float) -> dict:
    """Review rounds per merged PR the reviewer reviewed in the window, its open-to-merge
    time in seconds, and the change proofs run."""
    rounds = Counter(row["pr"] for row in rows["reviews"] if since <= row["created"] < until)
    merged = {pr: entry for pr, entry in (closed or {}).items()
              if pr in rounds and entry["merged"] is not None}
    waits = [e["merged"] - e["created"] for e in merged.values()
             if e["created"] is not None and e["merged"] >= e["created"]]
    return {"rounds": spread([rounds[pr] for pr in merged]) if closed is not None else None,
            "open_to_merge": spread(waits) if closed is not None else None,
            "change_proofs": sum(since <= row["created"] < until for row in rows["proofs"])}


def collect(loop: dict, db: Path, since: float, until: float | None = None,
            with_models: bool = False, listing=None) -> dict:
    """The public export. ``listing(loop, since)`` is ``closed_listing`` unless a test passes
    its own; ``with_models`` adds the reviewer-model comparison."""
    until = time.time() if until is None else until
    rows = read_ledger(Path(db), loop["repo"])
    closed, partial = (listing or closed_listing)(loop, since)
    turns = [row for row in rows["runs"] if since <= row["created"] < until]
    stopped, overruled = caught(rows, since, until)
    stamps = [row["created"] for row in turns]
    out = {"schema": SCHEMA, "repo": str(loop["repo"]),
           "window": {"first": day(min(stamps)) if stamps else None,
                      "last": day(max(stamps)) if stamps else None,
                      "prs": len({row["pr"] for row in turns}), "turns": len(turns)},
           "stopped": stopped, "overruled": overruled,
           "findings": findings(rows, closed, since, until),
           "cycle": cycle(rows, closed, since, until), "partial": bool(partial)}
    if with_models:
        from . import stats_public_models
        out["models"] = stats_public_models.compare(rows, since, until)
    return out
