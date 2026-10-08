"""``hermes dk stats``: what one loop did over a window, and how long it took.

Two sources. The run ledger (local, read-only) knows every seat turn: how it ended, how long it
ran and how long it waited to start — GitHub cannot say how long a turn took. GitHub (``--github``,
read as the reader) knows the PRs: opened, merged, open-to-merge time, time to first review and
review rounds. Output is totals and timings only: no error text, path, prompt or token, so the
JSON and HTML forms are safe to publish.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SEATS = ("reviewer", "fixer", "issue_fixer", "triage", "adjudicator")
STATES = ("succeeded", "failed", "waiting", "cancelled", "uncertain")
GITHUB_PAGES_MAX = 10
_SINCE = re.compile(r"^(\d{1,4})([hd])$")


def parse_since(text: str, now: float | None = None) -> float:
    """``7d``, ``24h`` or an ISO date (``2026-09-28``, UTC midnight) as an epoch; else ValueError."""
    now = time.time() if now is None else now
    text = str(text or "").strip()
    match = _SINCE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return now - amount * (3600 if unit == "h" else 86400)
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"--since {text!r}: use 7d, 24h or a date like 2026-09-28") from None
    return day.timestamp()


def summary(values: list[float]) -> dict | None:
    """n, mean, median, p90 and max of durations in seconds; None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(ordered), "mean": statistics.mean(ordered),
            "median": statistics.median(ordered), "p90": p90, "max": ordered[-1]}


def ledger(db: Path, repo: str, since: float) -> dict | None:
    """Per seat: turns by final state, how long succeeded turns ran, how long turns waited to
    start, and how many needed a retry. None when there is no ledger. Opened read-only."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        finished = "finished" if "finished" in columns else "NULL AS finished"
        retries = "retries" if "retries" in columns else "0 AS retries"
        rows = con.execute(f"SELECT seat, state, created, launch_intent, {finished}, updated, "
                           f"{retries}, error FROM runs WHERE repo=? AND created>=?",
                           (repo, since)).fetchall()
    finally:
        con.close()
    seats: dict[str, dict] = {}
    ran, waited = defaultdict(list), defaultdict(list)
    for row in rows:
        seat = seats.setdefault(row["seat"], {"turns": 0, "states": Counter(), "retried": 0,
                                              "held": 0})
        seat["turns"] += 1
        seat["states"][row["state"]] += 1
        seat["retried"] += 1 if (row["retries"] or 0) > 0 else 0
        if row["state"] == "waiting" and str(row["error"] or "").startswith("held:"):
            seat["held"] += 1
        start = row["launch_intent"]
        if start is None:
            continue
        if start >= row["created"]:
            waited[row["seat"]].append(start - row["created"])
        if row["state"] == "succeeded":
            end = row["finished"] if row["finished"] is not None else row["updated"]
            if end is not None and end >= start:
                ran[row["seat"]].append(end - start)
    for name, seat in seats.items():
        seat["states"] = dict(seat["states"])
        seat["ran"] = summary(ran[name])
        seat["waited"] = summary(waited[name])
    return seats


def revisions(db: Path, repo: str, since: float) -> dict | None:
    """Turns, verdicts and rounds grouped by prompt revision and by resolved model (#472). Rows
    from before the columns (or never launched) group as "unknown". A verdict is the review a
    turn posted (its receipt); rounds are the most turns one PR and seat took within the group."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        rev = "r.prompt_rev" if "prompt_rev" in columns else "NULL"
        model = "r.model" if "model" in columns else "NULL"
        try:
            rows = con.execute(
                f"SELECT {rev} AS prompt_rev, {model} AS model, r.seat, r.pr, x.verdict "
                "FROM runs r LEFT JOIN review_receipts x ON x.run_id=r.id AND x.state='posted' "
                "WHERE r.repo=? AND r.created>=?", (repo, since)).fetchall()
        except sqlite3.OperationalError:
            rows = con.execute(
                f"SELECT {rev} AS prompt_rev, {model} AS model, r.seat, r.pr, NULL AS verdict "
                "FROM runs r WHERE r.repo=? AND r.created>=?", (repo, since)).fetchall()
    finally:
        con.close()
    out = {}
    for kind in ("prompt_rev", "model"):
        groups: dict = {}
        for row in rows:
            g = groups.setdefault(row[kind] or "unknown",
                                  {"turns": 0, "verdicts": Counter(), "prs": Counter(),
                                   "seats": Counter()})
            g["turns"] += 1
            g["seats"][row["seat"]] += 1
            g["prs"][(row["seat"], row["pr"])] += 1
            if row["verdict"]:
                g["verdicts"][str(row["verdict"])] += 1
        out[kind] = {key: {"turns": g["turns"], "verdicts": dict(g["verdicts"]),
                           "seats": dict(g["seats"]), "rounds": summary(list(g["prs"].values()))}
                     for key, g in sorted(groups.items())}
    return out


def _iso(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def github(loop: dict, since: float) -> dict:
    """The PRs opened in the window and their reviews, read as the reader. ``partial`` says a
    read failed, so the counts are a lower bound."""
    from . import gh
    repo, reader = loop["repo"], loop.get("read_token")
    reviewers = {login.lower() for login in loop.get("reviewers") or []}
    prs, partial = [], False
    for page in range(1, GITHUB_PAGES_MAX + 1):
        batch = gh.api(loop, f"/repos/{repo}/pulls?state=all&sort=created&direction=desc"
                             f"&per_page=100&page={page}", login=reader)
        if not isinstance(batch, list):
            partial = True
            break
        fresh = [pr for pr in batch if isinstance(pr, dict)
                 and (_iso(pr.get("created_at")) or 0) >= since]
        prs.extend(fresh)
        if len(batch) < 100 or len(fresh) < len(batch):
            break
    else:
        partial = True
    authors, merge_times, first, rounds = Counter(), defaultdict(list), defaultdict(list), Counter()
    verdicts, merged, still_open = Counter(), 0, 0
    for pr in prs:
        opened = _iso(pr.get("created_at"))
        author = str((pr.get("user") or {}).get("login") or "?")
        authors[author] += 1
        if pr.get("merged_at"):
            merged += 1
            done = _iso(pr["merged_at"])
            if opened is not None and done is not None:
                merge_times[author].append(done - opened)
                merge_times["all"].append(done - opened)
        elif pr.get("state") == "open":
            still_open += 1
        reviews = gh.api(loop, f"/repos/{repo}/pulls/{pr.get('number')}/reviews?per_page=100",
                         login=reader)
        if not isinstance(reviews, list):
            partial = True
            continue
        seen, changes = set(), 0
        for review in sorted((r for r in reviews if isinstance(r, dict) and r.get("submitted_at")),
                             key=lambda r: r["submitted_at"]):
            login = str((review.get("user") or {}).get("login") or "?")
            verdicts[(login, str(review.get("state")))] += 1
            if login not in seen and opened is not None:
                seen.add(login)
                at = _iso(review["submitted_at"])
                if at is not None and at >= opened:
                    first[login].append(at - opened)
            if login.lower() in reviewers and review.get("state") == "CHANGES_REQUESTED":
                changes += 1
        if any(login.lower() in reviewers for login in seen):
            rounds[changes] += 1
    by_reviewer: dict[str, dict] = {}
    for (login, state), count in verdicts.items():
        by_reviewer.setdefault(login, {"reviews": 0, "states": {}})
        by_reviewer[login]["reviews"] += count
        by_reviewer[login]["states"][state] = count
    for login, waits in first.items():
        by_reviewer.setdefault(login, {"reviews": 0, "states": {}})["first_review"] = summary(waits)
    return {"opened": len(prs), "merged": merged, "open": still_open, "authors": dict(authors),
            "open_to_merge": {who: summary(times) for who, times in merge_times.items()},
            "reviewers": by_reviewer,
            "changes_requested_rounds": {str(k): v for k, v in sorted(rounds.items())},
            "partial": partial}


def collect(loop: dict, db: Path, since: float, with_github: bool,
            now: float | None = None) -> dict:
    now = time.time() if now is None else now
    report = {"repo": loop["repo"], "loop": loop.get("id", ""), "since": since, "until": now,
              "turns": ledger(db, loop["repo"], since),
              "revisions": revisions(db, loop["repo"], since)}
    if with_github:
        report["github"] = github(loop, since)
    return report


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.1f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _turn_rows(report: dict) -> list[list[str]]:
    rows = []
    turns = report.get("turns") or {}
    for seat in sorted(turns, key=lambda s: (SEATS.index(s) if s in SEATS else len(SEATS), s)):
        data = turns[seat]
        states = data["states"]
        other = data["turns"] - sum(states.get(s, 0) for s in STATES)
        ran, waited = data.get("ran"), data.get("waited")
        rows.append([seat, str(data["turns"]),
                     *(str(states.get(s, 0)) for s in STATES), str(other), str(data["retried"]),
                     duration(ran and ran["median"]), duration(ran and ran["mean"]),
                     duration(ran and ran["max"]), duration(waited and waited["median"])])
    return rows


TURN_HEAD = ["seat", "turns", *STATES, "other", "retried", "ran (median)", "ran (mean)",
             "ran (max)", "waited (median)"]


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(head, *rows)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return [line(head), *(line(row) for row in rows)]


def _github_parts(gh_report: dict) -> tuple[list[str], list[tuple[str, list[str], list]]]:
    """The GitHub section as notes and (title, head, rows) tables, for text and HTML alike."""
    notes = [f"PRs opened {gh_report['opened']} · merged {gh_report['merged']} · "
             f"open {gh_report['open']}"
             + ("  (a GitHub read failed: these are lower bounds)" if gh_report["partial"] else "")]
    if gh_report["authors"]:
        notes.append("by author: " + ", ".join(f"{who} {n}" for who, n in
                                               sorted(gh_report["authors"].items(),
                                                      key=lambda kv: -kv[1])))
    if gh_report["changes_requested_rounds"]:
        notes.append("change-request rounds per reviewed PR: " + ", ".join(
            f"{k}: {v}" for k, v in gh_report["changes_requested_rounds"].items()))
    tables = []
    merge = gh_report["open_to_merge"]
    rows = [[who, str(s["n"]), duration(s["median"]), duration(s["mean"]), duration(s["max"])]
            for who, s in sorted(merge.items(), key=lambda kv: (kv[0] != "all", kv[0])) if s]
    if rows:
        tables.append(("open → merge", ["author", "merged", "median", "mean", "max"], rows))
    rows = []
    for login, data in sorted(gh_report["reviewers"].items(), key=lambda kv: -kv[1]["reviews"]):
        states = data["states"]
        first = data.get("first_review")
        rows.append([login, str(data["reviews"]), str(states.get("APPROVED", 0)),
                     str(states.get("CHANGES_REQUESTED", 0)), str(states.get("COMMENTED", 0)),
                     duration(first and first["median"]), duration(first and first["mean"])])
    if rows:
        tables.append(("reviews", ["reviewer", "reviews", "approved", "changes", "commented",
                                   "first review (median)", "first review (mean)"], rows))
    return notes, tables


def text(report: dict) -> str:
    days = (report["until"] - report["since"]) / 86400
    out = [f"{report['repo']} (loop {report['loop']}) · since {_day(report['since'])} "
           f"({days:.1f} days)", ""]
    if report["turns"] is None:
        out.append("Turns: no run ledger yet")
    elif not report["turns"]:
        out.append("Turns: none in this window")
    else:
        out += ["Turns (run ledger: 'ran' is a succeeded turn's own time, 'waited' is queued "
                "until it started)", *_table(TURN_HEAD, _turn_rows(report))]
    for kind, title in (("prompt_rev", "prompt revision"), ("model", "model")):
        groups = (report.get("revisions") or {}).get(kind)
        if groups:
            rows = [[key, str(g["turns"]),
                     ", ".join(f"{n} {v}" for v, n in sorted(g["verdicts"].items())) or "-",
                     str(g["rounds"]["max"]) if g["rounds"] else "-"]
                    for key, g in groups.items()]
            out += ["", f"By {title} (verdicts posted; rounds = most turns on one PR and seat)",
                    *_table([title, "turns", "verdicts", "rounds (max)"], rows)]
    if "github" in report:
        notes, tables = _github_parts(report["github"])
        out += ["", "GitHub", *notes]
        for title, head, rows in tables:
            out += ["", title, *_table(head, rows)]
    return "\n".join(out)


def as_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


_CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#656d76;--line:#d8dee4;--card:#ffffff;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;
--line:#30363d;--card:#161b22;--accent:#4493f8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:1.4rem;margin:0 0 4px}h2{font-size:1.05rem;margin:28px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:1.5rem;color:var(--accent)}.card span{color:var(--muted)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}footer{color:var(--muted);margin-top:28px;font-size:.85rem}
"""


def _html_table(head: list[str], rows: list[list[str]]) -> str:
    cells = lambda tag, row: "".join(f"<{tag}>{html.escape(str(c))}</{tag}>" for c in row)  # noqa: E731
    body = "".join(f"<tr>{cells('td', row)}</tr>" for row in rows)
    return f'<div class="scroll"><table><tr>{cells("th", head)}</tr>{body}</table></div>'


def as_html(report: dict) -> str:
    """One self-contained page (no scripts, fonts or remote assets): publish it anywhere."""
    esc = html.escape
    turns = report.get("turns") or {}
    cards = []
    for seat in ("reviewer", "issue_fixer", "fixer"):
        ran = (turns.get(seat) or {}).get("ran")
        if ran:
            cards.append((duration(ran["median"]), f"median {seat.replace('_', ' ')} turn"))
    gh_report = report.get("github")
    if gh_report:
        cards[:0] = [(str(gh_report["opened"]), "PRs opened"), (str(gh_report["merged"]), "merged")]
        merge = gh_report["open_to_merge"].get("all")
        if merge:
            cards.append((duration(merge["median"]), "median open → merge"))
    parts = [f"<h1>{esc(report['repo'])}</h1>",
             f"<p class=\"sub\">Diaktoros activity since {esc(_day(report['since']))} · "
             f"generated {esc(_day(report['until']))}</p>"]
    if cards:
        parts.append('<div class="cards">' + "".join(
            f'<div class="card"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
            for value, label in cards) + "</div>")
    parts.append("<h2>Seat turns</h2>")
    if turns:
        parts.append(_html_table(TURN_HEAD, _turn_rows(report)))
    else:
        parts.append('<p class="sub">No turns in this window.</p>')
    if gh_report:
        notes, tables = _github_parts(gh_report)
        parts.append("<h2>Pull requests and reviews</h2>")
        parts += [f'<p class="sub">{esc(note)}</p>' for note in notes]
        for title, head, rows in tables:
            parts += [f"<h2>{esc(title)}</h2>", _html_table(head, rows)]
    parts.append('<footer>Generated by <a href="https://github.com/coe0718/diaktoros">'
                 "Diaktoros</a> <code>stats</code>. Totals and timings only.</footer>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Diaktoros stats</title><style>{_CSS}</style></head><body><main>"
            + "".join(parts) + "</main></body></html>\n")
