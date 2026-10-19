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

from .ledger import LOCK_WAIT_S

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


def ledger(db: Path, repo: str, since: float, until: float | None = None) -> dict | None:
    """Per seat: turns by final state, how long succeeded turns ran, how long turns waited to
    start, and how many needed a retry. None when there is no ledger. Opened read-only."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        finished = "finished" if "finished" in columns else "NULL AS finished"
        retries = "retries" if "retries" in columns else "0 AS retries"
        rows = con.execute(f"SELECT seat, state, created, launch_intent, {finished}, updated, "
                           f"{retries}, error FROM runs WHERE repo=? AND created>=? AND created<?",
                           (repo, since, float("inf") if until is None else until)).fetchall()
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


def usage(db: Path, repo: str, since: float) -> list[dict] | None:
    """Tokens in and out (and provider-reported cost) summed per seat and model (#480). Turns
    with no recorded tokens (rows from before, or never reached the proxy) are left out of the
    sums; ``costed`` counts the turns whose provider reported a cost, so a cost of 0 with
    ``costed`` 0 reads as unknown. None when there is no ledger or no token columns."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        if not {"tokens_in", "tokens_out", "cost"} <= columns:
            return None
        model = "model" if "model" in columns else "NULL"
        rows = con.execute(
            f"SELECT seat, COALESCE({model}, 'unknown'), COUNT(*), SUM(tokens_in), "
            "SUM(tokens_out), SUM(cost), COUNT(cost) FROM runs WHERE repo=? AND created>=? "
            "AND tokens_in IS NOT NULL GROUP BY seat, 2 ORDER BY seat, 2",
            (repo, since)).fetchall()
    finally:
        con.close()
    return [{"seat": seat, "model": model_name, "turns": turns, "tokens_in": t_in or 0,
             "tokens_out": t_out or 0, "cost": cost or 0.0, "costed": costed}
            for seat, model_name, turns, t_in, t_out, cost, costed in rows]


def _usage_rows(report: dict) -> list[list[str]]:
    return [[row["seat"], row["model"], str(row["turns"]), f"{row['tokens_in']:,}",
             f"{row['tokens_out']:,}",
             f"{row['cost']:.4f}" if row["costed"] else "unknown"]
            for row in report.get("usage") or []]


USAGE_HEAD = ["seat", "model", "turns", "tokens in", "tokens out", "cost (provider-reported)"]


def revisions(db: Path, repo: str, since: float) -> dict | None:
    """Turns, verdicts and rounds grouped by prompt revision and by resolved model (#472). Rows
    from before the columns (or never launched) group as "unknown". A verdict is the review a
    turn posted (its receipt); rounds are the most turns one PR and seat took within the group."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        rev = "r.prompt_rev" if "prompt_rev" in columns else "NULL"
        model = "r.model" if "model" in columns else "NULL"
        think = "r.thinking" if "thinking" in columns else "NULL"
        try:
            rows = con.execute(
                f"SELECT {rev} AS prompt_rev, {model} AS model, {think} AS thinking, r.seat, r.pr, x.verdict "
                "FROM runs r LEFT JOIN review_receipts x ON x.run_id=r.id AND x.state='posted' "
                "WHERE r.repo=? AND r.created>=? AND r.seat!='shadow'", (repo, since)).fetchall()
        except sqlite3.OperationalError:
            rows = con.execute(
                f"SELECT {rev} AS prompt_rev, {model} AS model, {think} AS thinking, r.seat, r.pr, NULL AS verdict "
                "FROM runs r WHERE r.repo=? AND r.created>=? AND r.seat!='shadow'",
                (repo, since)).fetchall()
    finally:
        con.close()
    out = {}
    for kind in ("prompt_rev", "model", "thinking"):
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


LIVE_STATES = ("pending", "claimed", "launching", "running", "waiting", "uncertain")
RECENT_FAILED_S = 24 * 3600
WHY_MAX = 160


def _why(state: str, error) -> str:
    """A short, host-written reason: a hold's own line, else the state's meaning. A failed or
    uncertain run's error is not repeated here (``explain`` shows it); never a turn's output."""
    text = str(error or "")
    if state == "waiting" and text.startswith("held:"):
        return text[:WHY_MAX]
    return {"waiting": "waiting to retry", "uncertain": "needs an operator: may have written",
            "failed": "failed: see explain", "pending": "queued", "claimed": "starting",
            "launching": "starting", "running": "running",
            "cancelled": "cancelled before any write — retry re-arms it"}.get(state, state)


NOW_PAGE = 100          # rows of ``runs`` that ``now`` returns; the counts are never capped


def _now_filter(at: float) -> tuple[str, tuple]:
    """The WHERE clause that says what ``now`` covers (``repo`` is the first argument, then
    these): every live run, the policy-cancelled fixer runs an operator ``retry`` recovers (the
    one cancellation ``retry_action.offered`` draws the same line at; any other cancelled run is
    only history) and the last day's failures. ``now``'s page, its counts and ``stuck`` all read
    through it."""
    from .run_supervisor import POLICY_CANCELLATIONS
    marks = ",".join("?" * len(LIVE_STATES))
    cancelled = ",".join("?" * len(POLICY_CANCELLATIONS))
    return (f"repo=? AND seat!='shadow' AND (state IN ({marks}) "
            f"OR (state='cancelled' AND error IN ({cancelled})) "
            "OR (state='failed' AND updated>=?))",
            (*LIVE_STATES, *POLICY_CANCELLATIONS, at - RECENT_FAILED_S))


def _now_run(row) -> dict:
    return {"pr": row["pr"], "seat": row["seat"], "head": str(row["head"] or "")[:7],
            "state": row["state"], "why": _why(row["state"], row["error"]),
            "since": row["updated"]}


def now(db: Path, repo: str, at: float | None = None) -> dict | None:
    """What the loop is doing right now, for the Desktop page: every live run (queued, running,
    held, uncertain), the fixer runs the push policy cancelled (dead for their head until an
    operator ``retry`` recovers them, which is a thing to act on now), and the last day's
    failures, newest first. None when there is no ledger. Opened read-only.

    ``counts`` and ``total`` cover the whole set. ``runs`` is the newest ``NOW_PAGE`` of it;
    ``truncated`` says it is a page, not everything (``total`` > ``len(runs)``).
    """
    db = Path(db)
    if not db.exists():
        return None
    at = time.time() if at is None else at
    where, args = _now_filter(at)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(f"SELECT pr, seat, head, state, error, updated FROM runs WHERE {where} "
                           "ORDER BY updated DESC LIMIT ?", (repo, *args, NOW_PAGE)).fetchall()
        # ``held`` is the test ``_why`` makes: a waiting run whose error starts "held:".
        tally = con.execute("SELECT state, (state='waiting' AND substr(COALESCE(error,''),1,5)"
                            f"='held:') AS held, COUNT(*) AS n FROM runs WHERE {where} "
                            "GROUP BY 1, 2", (repo, *args)).fetchall()
    finally:
        con.close()
    counts: Counter = Counter()
    for row in tally:
        counts["held" if row["held"]
               else "running" if row["state"] in ("claimed", "launching", "running")
               else row["state"]] += row["n"]
    total = sum(counts.values())
    return {"repo": repo, "at": at, "runs": [_now_run(row) for row in rows],
            "counts": dict(counts), "total": total, "truncated": total > len(rows)}


def _iso(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def github(loop: dict, since: float) -> dict:
    """The PRs opened in the window and their reviews, read as the reader. ``partial`` says a
    read failed; ``capped`` says the window held more PRs than the GITHUB_PAGES_MAX pages
    read, with no read failing. Either way the counts are a lower bound."""
    from . import gh
    repo, reader = loop["repo"], loop.get("read_token")
    reviewers = {login.lower() for login in loop.get("reviewers") or []}
    prs, partial, capped = [], False, False
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
        capped = True
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
        reviews, _error = gh.reviews_read(loop, pr.get("number"), login=reader)
        if reviews is None:
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
            "partial": partial, "capped": capped}


def collect(loop: dict, db: Path, since: float, with_github: bool,
            now: float | None = None) -> dict:
    now = time.time() if now is None else now
    report = {"repo": loop["repo"], "loop": loop.get("id", ""), "since": since, "until": now,
              "turns": ledger(db, loop["repo"], since),
              "revisions": revisions(db, loop["repo"], since),
              "usage": usage(db, loop["repo"], since)}
    try:
        from . import findings
        report["findings_missed"] = findings.missed_count(loop, since, now)
    except Exception:
        report["findings_missed"] = None
    if with_github:
        report["github"] = github(loop, since)
    return report


def review_window(db: Path, repo: str, since: float, until: float) -> dict | None:
    """Reviews and rulings in ``[since, until)`` from the run ledger alone: PRs reviewed,
    verdicts, rounds per PR, request-to-verdict time, rulings, and per resolved model the turns,
    failures, verdicts, time to verdict and rounds. None when there is no ledger."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        model = "r.model" if "model" in columns else "NULL"
        runs = con.execute(f"SELECT r.id, r.pr, r.seat, r.state, {model} AS model, r.created "
                           "FROM runs r WHERE r.repo=? AND r.created>=? AND r.created<? "
                           "AND r.seat!='shadow'", (repo, since, until)).fetchall()
        try:
            receipts = con.execute("SELECT run_id, verdict, created, confirmed FROM review_receipts "
                                   "WHERE state IN ('posted','confirmed')").fetchall()
        except sqlite3.OperationalError:
            receipts = []
        try:
            rulings = con.execute("SELECT verdict FROM rulings WHERE repo=? AND created>=? "
                                  "AND created<?", (repo, since, until)).fetchall()
        except sqlite3.OperationalError:
            rulings = []
    finally:
        con.close()
    receipt = {row["run_id"]: row for row in receipts}
    names = {"APPROVE": "approved", "APPROVED": "approved", "REQUEST_CHANGES": "changes",
             "CHANGES_REQUESTED": "changes"}
    verdicts, per_pr, waits = Counter(), Counter(), []
    models: dict[str, dict] = {}
    for run in runs:
        group = models.setdefault(run["model"] or "unknown",
                                  {"turns": 0, "failed": 0, "verdicts": Counter(),
                                   "prs": Counter(), "waits": []})
        group["turns"] += 1
        group["failed"] += 1 if run["state"] == "failed" else 0
        got = receipt.get(run["id"]) if run["seat"] == "reviewer" else None
        if got is None:
            continue
        end = got["confirmed"] if got["confirmed"] is not None else got["created"]
        name = names.get(str(got["verdict"]), "other")
        verdicts[name] += 1
        per_pr[run["pr"]] += 1
        group["verdicts"][name] += 1
        group["prs"][run["pr"]] += 1
        if end is not None and end >= run["created"]:
            waits.append(end - run["created"])
            group["waits"].append(end - run["created"])
    return {"prs_reviewed": len(per_pr), "verdicts": dict(verdicts),
            "rounds": summary(list(per_pr.values())), "to_verdict": summary(waits),
            "rulings": dict(Counter(str(row["verdict"]) for row in rulings)),
            "models": {name: {"turns": g["turns"], "failed": g["failed"],
                              "verdicts": dict(g["verdicts"]),
                              "to_verdict": summary(g["waits"]),
                              "rounds": summary(list(g["prs"].values()))}
                       for name, g in sorted(models.items())}}


def escalations(db: Path, repo: str, since: float, until: float,
                breaches: dict | None = None) -> dict | None:
    """PRs sent for a ruling in ``[since, until)`` (#659), counted apart from the rulings.

    An escalation is an adjudicator turn in the run ledger: the cap breach and ``escalate`` both
    commit one, keyed ``breach:<rounds>`` at the head, and the ledger's unique turn index makes a
    redelivered breach the same row, so a retry never counts twice while a re-breach at a new head
    is a new one. ``ruled`` is those with a ruling, at any time; ``awaiting`` the rest. Who asked
    comes from the loop's breach markers (``breaches``, keyed ``repo#pr``), which keep only each
    PR's latest head: an older escalation, or one whose marker is gone, is ``unknown``, never
    guessed. A marker in the window with no turn (no adjudicator configured) counts as well.
    None when there is no ledger."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, pr, head, created FROM runs WHERE repo=? "
                           "AND seat='adjudicator' AND created>=? AND created<?",
                           (repo, since, until)).fetchall()
        try:
            ruled = {row[0] for row in con.execute("SELECT run_id FROM rulings WHERE repo=?",
                                                   (repo,))}
        except sqlite3.OperationalError:
            ruled = set()
    finally:
        con.close()
    markers = {}
    for key, marker in (breaches or {}).items():
        prefix, _, number = str(key).rpartition("#")
        if prefix == repo and number.isdigit() and isinstance(marker, dict):
            markers[int(number)] = marker

    def by(pr: int, head: str) -> str:
        marker = markers.get(pr) or {}
        if marker.get("head") != head:
            return "unknown"
        return "operator" if marker.get("escalated") else "cap"

    counts = Counter()
    seen = set()
    for row in rows:
        seen.add((row["pr"], row["head"]))
        counts["ruled" if row["id"] in ruled else "awaiting"] += 1
        counts[by(row["pr"], row["head"])] += 1
    for pr, marker in markers.items():
        at = _iso(marker.get("at"))
        if (at is not None and since <= at < until and (pr, marker.get("head")) not in seen):
            counts["awaiting"] += 1                 # no adjudicator turn: a person rules
            counts[by(pr, marker.get("head"))] += 1
    return {"total": counts["ruled"] + counts["awaiting"], "ruled": counts["ruled"],
            "awaiting": counts["awaiting"],
            "by": {kind: counts[kind] for kind in ("cap", "operator", "unknown")}}


FINDING_OUTCOMES = ("fixed", "withdrawn", "overruled", "upheld", "respecified", "disputed", "open")


def finding_outcomes(db: Path, repo: str, since: float, until: float,
                     tracked: dict | None) -> dict | None:
    """What became of the reviewer's numbered findings raised in ``[since, until)`` (#660).

    ``tracked`` is the loop's finding record (``LoopState.findings_all``: per PR, each ``F<n>``
    with its latest state, the head it was raised at and when). A finding keeps its id across
    rounds, so it counts once however many reviews restate it. Its outcome, first match wins:

    - ``fixed`` / ``withdrawn``: the reviewer's own latest state for it. A ruling never claims
      such a finding: it rules on the findings still open, and this one was settled by then or
      at least is now;
    - ``upheld``: still open, and a REJECT ruling on its PR after it was raised (at that head or
      a later one: a finding still open was outstanding at every head since);
    - ``overruled``: still open, and an ACCEPT ruling after it (the adjudicator found it did
      not block: the one adjudication-confirmed false positive);
    - ``respecified``: still open, and a RESPEC ruling after it (the goal was in question);
    - ``disputed``: the fixer disputed the round after it was raised and no ruling followed;
    - ``open``: none of those yet.

    A dispute is not proof of a false positive, and an unchallenged finding is not proof of a
    true one: ``false_positives`` is bounded to what a ruling decided (overruled over overruled
    plus upheld). Per resolved model of the review that raised it (the reviewer run at that
    head), else ``unknown``. A finding recorded before its time was kept (no ``at``) cannot be
    placed in a window: ``undated`` counts them, outside every window. None without a ledger."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        columns = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        model = "model" if "model" in columns else "NULL"
        reviews = con.execute(f"SELECT pr, head, {model} AS model FROM runs WHERE repo=? "
                              "AND seat='reviewer' ORDER BY created", (repo,)).fetchall()
        try:
            rulings = con.execute("SELECT pr, verdict, created FROM rulings WHERE repo=? "
                                  "ORDER BY created", (repo,)).fetchall()
        except sqlite3.OperationalError:
            rulings = []
        try:
            disputes = con.execute("SELECT pr, created FROM disputes WHERE repo=?",
                                   (repo,)).fetchall()
        except sqlite3.OperationalError:
            disputes = []
    finally:
        con.close()
    models = {(row["pr"], row["head"]): row["model"] for row in reviews}   # the latest wins
    counts, by_model, undated = Counter(), {}, 0
    for number, entry in (tracked or {}).items():
        if not str(number).isdigit() or not isinstance(entry, dict):
            continue
        pr = int(number)
        for item in (entry.get("findings") or {}).values():
            if not isinstance(item, dict):
                continue
            at = item.get("at")
            if not isinstance(at, (int, float)):
                undated += 1
                continue
            if not since <= at < until:
                continue
            state = item.get("state")
            # A ruling decides the findings still open when it runs, whatever head it judged. The
            # record keeps each finding's latest state, so only one still open now was open at
            # every ruling since it was raised; a fixed or withdrawn one keeps its own outcome.
            ruled = (next((r["verdict"] for r in rulings if r["pr"] == pr and r["created"] >= at),
                          None) if state not in ("fixed", "withdrawn") else None)
            if ruled == "REJECT":
                outcome = "upheld"
            elif ruled == "ACCEPT":
                outcome = "overruled"
            elif ruled == "RESPEC":
                outcome = "respecified"
            elif state in ("fixed", "withdrawn"):
                outcome = state
            elif any(d["pr"] == pr and d["created"] >= at for d in disputes):
                outcome = "disputed"
            else:
                outcome = "open"
            counts[outcome] += 1
            name = models.get((pr, item.get("head"))) or "unknown"
            by_model.setdefault(name, Counter())[outcome] += 1

    def shaped(c: Counter) -> dict:
        return {"raised": sum(c.values()), **{name: c[name] for name in FINDING_OUTCOMES},
                "false_positives": {"overruled": c["overruled"],
                                    "ruled": c["overruled"] + c["upheld"]}}
    return {**shaped(counts), "undated": undated,
            "models": {name: shaped(c) for name, c in sorted(by_model.items())}}


def regressions(db: Path, repo: str, since: float, until: float) -> dict | None:
    """Approved, then broke (#661): the bugs linked in ``[since, until)`` (when the link was
    recorded, which is when the attribution became known) to an approved PR that shipped them.

    Only links ``hermes dk regression`` verified against GitHub exist, so nothing here is
    guessed from text. The headline counts approved PRs, not bug reports: three issues about one
    regression are one PR that broke. A retracted link is left out and counted as ``retracted``;
    re-recording a link is the same row. ``from`` is when the first link was ever recorded: the
    table starts with this version, so earlier regressions are simply not linked, not zero.
    None without a ledger."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute("SELECT issue, pr, head, model, state FROM regressions WHERE repo=? "
                               "AND created>=? AND created<? ORDER BY pr, issue",
                               (repo, since, until)).fetchall()
            first = con.execute("SELECT MIN(created) FROM regressions WHERE repo=?",
                                (repo,)).fetchone()[0]
        except sqlite3.OperationalError:
            rows, first = [], None
    finally:
        con.close()
    live = [row for row in rows if row["state"] == "linked"]
    models = {}
    for row in live:
        models.setdefault(row["model"] or "unknown", set()).add(row["pr"])
    return {"prs": len({row["pr"] for row in live}), "issues": len(live),
            "retracted": len(rows) - len(live),
            "models": {name: len(prs) for name, prs in sorted(models.items())},
            "links": [{"issue": row["issue"], "pr": row["pr"], "head": row["head"][:7],
                       "model": row["model"] or "unknown"} for row in live],
            "from": first}


FIX_READS_MAX = 100


def fix_outcomes(db: Path, repo: str, since: float, until: float, pr_state) -> dict | None:
    """What became of the issue fixes proposed in ``[since, until)`` (#678).

    The cohort is the issue-fix writes the ledger recorded in the window, dated when they were
    recorded: a PR opened last week and merged this week stays in last week's cohort, and its
    outcome is read now, so a later merge updates it. A PR's outcome is GitHub's (``pr_state``:
    ``open``, ``merged``, ``closed`` or None when unreadable); a write with no PR number (its
    outcome uncertain) or an unreadable PR is ``unknown``, never guessed. Conversion is merged
    over decided (merged + closed), with open and unknown shown beside it.

    Turns per merged fix: the launched issue-fix turn plus every launched turn on its PR (review,
    fix, CI fix, conflict, ruling) — ledger rows, one per turn, so a redelivery cannot add one.
    Operator steps are the ``operator_actions`` on its PR after the proposal; the table exists
    from this version on, so ``steps_from`` is the first one recorded and older work reads as a
    lower bound. None when there is no ledger."""
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        try:
            fixes = con.execute("SELECT run_id, number, kind, pr_number, created FROM issue_fixes "
                                "WHERE repo=? AND created>=? AND created<? ORDER BY created",
                                (repo, since, until)).fetchall()
        except sqlite3.OperationalError:
            fixes = []
        prs = sorted({row["pr_number"] for row in fixes if row["kind"] == "pr"
                      and row["pr_number"] is not None})
        marks = ",".join("?" * len(prs))
        runs = con.execute(f"SELECT id, pr, created FROM runs WHERE repo=? AND pr IN ({marks}) "
                           "AND seat NOT IN ('issue_fixer','shadow') AND launch_intent IS NOT NULL",
                           (repo, *prs)).fetchall() if prs else []
        launched = {row[0] for row in con.execute(
            "SELECT id FROM runs WHERE repo=? AND seat='issue_fixer' AND launch_intent IS NOT NULL",
            (repo,))}
        try:
            actions = con.execute(f"SELECT pr, action, created FROM operator_actions WHERE repo=? "
                                  f"AND pr IN ({marks})", (repo, *prs)).fetchall() if prs else []
            first = con.execute("SELECT MIN(created) FROM operator_actions WHERE repo=?",
                                (repo,)).fetchone()[0]
        except sqlite3.OperationalError:
            actions, first = [], None
    finally:
        con.close()
    outcomes, turns, steps = Counter(), [], Counter()
    explained, partial, reads = 0, False, {}
    for fix in fixes:
        if fix["kind"] != "pr":
            explained += 1
            continue
        number = fix["pr_number"]
        if number is None:
            outcomes["unknown"] += 1
            continue
        if number not in reads:
            if len(reads) >= FIX_READS_MAX:
                partial = True
                reads[number] = None
            else:
                try:
                    reads[number] = pr_state(number)
                except Exception:
                    reads[number] = None
        outcome = reads[number] or "unknown"
        outcomes[outcome] += 1
        mine = [a for a in actions if a["pr"] == number and a["created"] >= fix["created"]]
        steps.update(a["action"] for a in mine)
        if outcome == "merged":
            turns.append((1 if fix["run_id"] in launched else 0)
                         + sum(1 for run in runs if run["pr"] == number
                               and run["created"] >= fix["created"]))
    decided = outcomes["merged"] + outcomes["closed"]
    return {"proposed": sum(outcomes.values()), "explained": explained,
            "outcomes": {name: outcomes[name] for name in ("merged", "closed", "open", "unknown")},
            "conversion": {"merged": outcomes["merged"], "decided": decided},
            "turns_per_merged": summary(turns), "steps": dict(sorted(steps.items())),
            "steps_from": first, "partial": partial}


def stuck(db: Path, repo: str, at: float) -> dict | None:
    """The longest-waiting run that is held, waiting or uncertain, with ``now``'s host-written
    reason; None when nothing is stuck. The ledger is asked for the oldest one, so it is the
    oldest of all of them, not of the page ``now`` returns."""
    db = Path(db)
    if not db.exists():
        return None
    where, args = _now_filter(at)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(f"SELECT pr, seat, head, state, error, updated FROM runs WHERE {where} "
                          "AND state IN ('waiting','uncertain') ORDER BY updated ASC LIMIT 1",
                          (repo, *args)).fetchone()
    finally:
        con.close()
    return _now_run(row) if row else None


def _breaches(loop: dict) -> dict:
    """The loop's breach markers, read-only; empty when they cannot be read."""
    try:
        from . import state as state_mod
        st = state_mod.state_for(loop)
        data = json.loads(Path(st.breach).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# A finding's citation, ``path/to/file.ext:12`` (the line optional): what two reviews of the same
# head are matched on (#218). Two citations of one file match when either names no line or the
# lines are within this many of each other.
_CITE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_./-]*\.[A-Za-z0-9]+)(?::(\d+))?")
CITE_NEAR = 10
_VERDICT = {"APPROVE": "approved", "APPROVED": "approved", "REQUEST_CHANGES": "changes",
            "CHANGES_REQUESTED": "changes"}


def _citation(text: str) -> tuple[str, int | None] | None:
    found = _CITE.search(str(text or ""))
    if not found:
        return None
    return found.group(1).lstrip("./"), int(found.group(2)) if found.group(2) else None


def _matches(a, b) -> bool:
    return (a is not None and b is not None and a[0] == b[0]
            and (a[1] is None or b[1] is None or abs(a[1] - b[1]) <= CITE_NEAR))


def _match_findings(shadow: list, real: list) -> tuple[int, int, int]:
    """``(matched, shadow only, reviewer only)``: each finding pairs with at most one."""
    left = [_citation(t) for t in real]
    matched = 0
    for text in shadow:
        cite = _citation(text)
        hit = next((i for i, other in enumerate(left) if _matches(cite, other)), None)
        if hit is not None:
            matched += 1
            left.pop(hit)
    return matched, len(shadow) - matched, len(left)


def _real_verdict(con, repo: str, pr: int, head: str) -> str | None:
    """The real reviewer's posted verdict at this head (``approved``/``changes``), or None."""
    got = con.execute(
        "SELECT x.verdict FROM runs r JOIN review_receipts x ON x.run_id=r.id "
        "WHERE r.repo=? AND r.pr=? AND r.head=? AND r.seat='reviewer' "
        "AND x.state IN ('posted','confirmed') ORDER BY x.created DESC LIMIT 1",
        (repo, pr, head)).fetchone()
    return None if got is None else _VERDICT.get(str(got[0]), "other")


SHADOW_SHOWN = 10


def shadow_reviews_for(db: Path, repo: str, pr: int) -> tuple[list[dict], int]:
    """One PR's shadow reviews (#218), newest first, for `explain` and the Desktop drawer: the
    model, head, would-be verdict, its first new findings, a human_paths hold, and the real
    reviewer's verdict at that head (None while there is none) — at most ``SHADOW_SHOWN``, with
    how many older ones were left out. Read-only; ``([], 0)`` without a ledger or a review."""
    from . import findings as findings_mod, run_supervisor
    # Through the one read-only door: `explain` and the Desktop drawer create no -wal/-shm files.
    con = run_supervisor.read_only_connect(db)
    if con is None:
        return [], 0
    try:
        try:
            rows = con.execute("SELECT * FROM shadow_reviews WHERE repo=? AND pr=? "
                               "ORDER BY created DESC, run_id LIMIT ?",
                               (repo, pr, SHADOW_SHOWN)).fetchall()
            total = con.execute("SELECT COUNT(*) FROM shadow_reviews WHERE repo=? AND pr=?",
                                (repo, pr)).fetchone()[0]
        except sqlite3.OperationalError:
            return [], 0
        out = []
        for row in rows:
            mine = _VERDICT.get(str(row["verdict"]), "other")
            real = _real_verdict(con, repo, pr, row["head"])
            own = [rest.lstrip(" :")[:200] for _, kind, rest
                   in findings_mod.parse(row["body"] or "") if kind == "new"]
            out.append({"model": str(row["model"] or "unknown"), "head": str(row["head"])[:7],
                        "verdict": mine, "real": real,
                        "agrees": None if real is None else real == mine,
                        "findings": own[:3], "more": max(0, len(own) - 3),
                        "held": str(row["held"] or ""), "created": row["created"]})
        return out, max(0, total - len(out))
    finally:
        con.close()


def shadow_outcomes(db: Path, repo: str, since: float, until: float,
                    tracked: dict | None) -> dict | None:
    """Shadow reviews (#218) recorded in ``[since, until)``, each set beside the real review at
    the same head: verdict agreement per shadow model, the disagreements both ways, and findings
    matched by cited file and line. The real verdict is the reviewer run's posted receipt at that
    head; its findings are the ones the loop's finding record (#475) opened at that head. A
    shadow whose head has no real verdict yet is pending. None when there is no ledger; ``{}``
    when it holds no shadow reviews."""
    from . import findings as findings_mod
    db = Path(db)
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        con.row_factory = sqlite3.Row
        try:
            shadows = con.execute("SELECT * FROM shadow_reviews WHERE repo=? AND created>=? "
                                  "AND created<? ORDER BY created, run_id",
                                  (repo, since, until)).fetchall()
        except sqlite3.OperationalError:
            return {}
        real = {}
        for row in shadows:
            verdict = _real_verdict(con, repo, row["pr"], row["head"])
            if verdict is not None:
                real[row["run_id"]] = verdict
    finally:
        con.close()
    if not shadows:
        return {}
    models: dict = {}
    blocked_approved, approved_blocked = [], []
    totals = [0, 0, 0]
    for row in shadows:
        model = models.setdefault(row["model"] or "unknown",
                                  {"shadowed": 0, "paired": 0, "agreed": 0})
        model["shadowed"] += 1
        verdict = real.get(row["run_id"])
        if verdict is None:
            continue
        model["paired"] += 1
        mine = _VERDICT.get(row["verdict"], "other")
        model["agreed"] += mine == verdict
        theirs = [item.get("text") or "" for item in
                  ((tracked or {}).get(str(row["pr"])) or {}).get("findings", {}).values()
                  if isinstance(item, dict) and item.get("head") == row["head"]]
        own = [rest for _, kind, rest in findings_mod.parse(row["body"] or "") if kind == "new"]
        for i, n in enumerate(_match_findings(own, theirs)):
            totals[i] += n
        case = {"pr": row["pr"], "head": row["head"], "model": row["model"],
                "findings": [f.lstrip(" :")[:120] for f in (own if mine == "changes" else theirs)][:3]}
        if mine == "changes" and verdict == "approved":
            blocked_approved.append(case)
        elif mine == "approved" and verdict == "changes":
            approved_blocked.append(case)
    return {"shadowed": len(shadows), "paired": sum(m["paired"] for m in models.values()),
            "agreed": sum(m["agreed"] for m in models.values()), "models": models,
            "blocked_approved": blocked_approved, "approved_blocked": approved_blocked,
            "findings": dict(zip(("matched", "shadow_only", "reviewer_only"), totals))}


def _tracked_findings(loop: dict) -> dict:
    """The loop's numbered findings record, read-only; empty when it cannot be read."""
    try:
        from . import state as state_mod
        return state_mod.state_for(loop).findings_all()
    except Exception:
        return {}


def report(loop: dict, db: Path, since: float, until: float | None = None,
           breaches: dict | None = None, pr_state=None, tracked: dict | None = None) -> dict:
    """The digest for one loop over ``[since, until)`` plus the equal window before it, for
    week-over-week trends. The run ledger and the loop's breach markers, plus one GitHub read
    (as the reader) per issue-fix PR in the window for its outcome."""
    until = time.time() if until is None else until
    before = since - (until - since)
    repo = loop["repo"]
    breaches = _breaches(loop) if breaches is None else breaches
    tracked = _tracked_findings(loop) if tracked is None else tracked
    if pr_state is None:
        from . import fix_hold

        def pr_state(number):
            return fix_hold.pr_state(loop, number)
    return {"repo": repo, "loop": loop.get("id", ""), "since": since, "until": until,
            "turns": ledger(db, repo, since, until),
            "turns_before": ledger(db, repo, before, since),
            "reviews": review_window(db, repo, since, until),
            "reviews_before": review_window(db, repo, before, since),
            "escalations": escalations(db, repo, since, until, breaches),
            "escalations_before": escalations(db, repo, before, since, breaches),
            "fixes": fix_outcomes(db, repo, since, until, pr_state),
            "findings": finding_outcomes(db, repo, since, until, tracked),
            "regressions": regressions(db, repo, since, until),
            "shadows": shadow_outcomes(db, repo, since, until, tracked),
            "stuck": stuck(db, repo, until)}


WEEK_S = 7 * 86400


def weekly(loop: dict, st, db: Path, now: float | None = None) -> bool:
    """The watchdog's weekly report (#658): when ``observer.weekly_report`` is set and its
    latest scheduled instant has no report yet, send ``report`` for the seven days before it.
    The same calculation as ``hermes dk report``. A missed week (the host was off) is not sent
    late: only the latest due window is. No model, no seat turn. True when one was delivered."""
    from . import observer
    spec = (loop.get("observer") or {}).get("weekly_report")
    if not spec or observer.configured(loop):
        return False
    now = time.time() if now is None else now
    due = observer.weekly_due(spec, now)
    if due is None:
        return False
    identity = _day(due)
    if observer.report_claimed(loop, st, identity):
        return False
    text = "📊 weekly report\n" + report_text(report(loop, db, due - WEEK_S, due))
    return observer.send_report(loop, st, identity, text)


def _total(seats: dict | None, state: str | None = None) -> int:
    return sum(seat["turns"] if state is None else seat["states"].get(state, 0)
               for seat in (seats or {}).values())


def _trend(value: int, before: int) -> str:
    return f"{value} ({value - before:+d} vs previous)"


def report_text(data: dict) -> str:
    days = (data["until"] - data["since"]) / 86400
    out = [f"{data['repo']} (loop {data['loop']}) · {_day(data['since'])} → "
           f"{_day(data['until'])} ({days:.1f} days)"]
    reviews = data["reviews"]
    if reviews is None:
        return "\n".join(out + ["no run ledger yet"])
    prior = data["reviews_before"] or {}
    verdicts, old = reviews["verdicts"], prior.get("verdicts", {})
    out.append("PRs reviewed: " + _trend(reviews["prs_reviewed"], prior.get("prs_reviewed", 0)))
    out.append("verdicts: approved " + _trend(verdicts.get("approved", 0), old.get("approved", 0))
               + " · changes requested "
               + _trend(verdicts.get("changes", 0), old.get("changes", 0)))
    rounds, wait = reviews["rounds"], reviews["to_verdict"]
    out.append(f"review rounds per PR: median {rounds['median']:.0f}, max {rounds['max']}"
               if rounds else "review rounds per PR: -")
    out.append(f"request → verdict: median {duration(wait and wait['median'])}, "
               f"max {duration(wait and wait['max'])}")
    rulings = reviews["rulings"]
    escalated, was_escalated = data.get("escalations"), data.get("escalations_before") or {}
    if escalated is not None:
        by = escalated["by"]
        out.append(f"escalations: {_trend(escalated['total'], was_escalated.get('total', 0))}"
                   f" · ruled {escalated['ruled']}, awaiting {escalated['awaiting']}"
                   f" · by cap {by['cap']}, by operator {by['operator']}, unknown {by['unknown']}")
    out.append("rulings: " + (", ".join(f"{n} {v}" for v, n in sorted(rulings.items()))
                              if rulings else "none"))
    found = data.get("findings")
    if found is not None:
        fp = found["false_positives"]
        out.append(f"findings: {found['raised']} raised · "
                   + ", ".join(f"{name} {found[name]}" for name in FINDING_OUTCOMES)
                   + f" · adjudicated false positives: {fp['overruled']} of {fp['ruled']} ruled"
                   + (f" · {found['undated']} undated, in no window" if found["undated"] else ""))
    broke = data.get("regressions")
    if broke is not None:
        out.append(f"approved, then broke: {broke['prs']} PRs ({broke['issues']} bug reports)"
                   + (" · " + ", ".join(f"{name} {n}" for name, n in broke["models"].items())
                      if broke["models"] else "")
                   + (f" · {broke['retracted']} retracted" if broke["retracted"] else "")
                   + (f" · linked from {_day(broke['from'])}" if broke["from"]
                      else " · none linked yet (`hermes dk regression`)"))
        out += [f"  #{link['issue']} ← PR #{link['pr']} approved at {link['head']} "
                f"({link['model']})" for link in broke["links"][:10]]
    shadows = data.get("shadows")
    if shadows:
        paired = shadows["findings"]
        out.append(f"shadow reviews: {shadows['shadowed']} ({shadows['paired']} beside a real "
                   f"verdict) · verdicts agreed {shadows['agreed']}/{shadows['paired']} · "
                   + ", ".join(f"{name} {m['agreed']}/{m['paired']}"
                               for name, m in shadows["models"].items())
                   + f" · findings: {paired['matched']} matched, {paired['shadow_only']} shadow "
                     f"only, {paired['reviewer_only']} reviewer only")
        for label, cases in (("shadow blocked, reviewer approved", shadows["blocked_approved"]),
                             ("shadow approved, reviewer blocked", shadows["approved_blocked"])):
            out += [f"  {label}: #{case['pr']} at {str(case['head'])[:7]}"
                    + (f" ({case['findings'][0]})" if case["findings"] else "")
                    for case in cases[:10]]
    fixes = data.get("fixes")
    if fixes is not None:
        got, conv, per = fixes["outcomes"], fixes["conversion"], fixes["turns_per_merged"]
        steps = fixes["steps"]
        out.append(f"issue fixes: {fixes['proposed']} PRs proposed"
                   f" ({fixes['explained']} explained without a PR) · merged {got['merged']},"
                   f" closed {got['closed']}, open {got['open']}, unknown {got['unknown']}"
                   + (" (some PRs not read)" if fixes["partial"] else ""))
        out.append(f"merge conversion: {conv['merged']}/{conv['decided']} decided"
                   f" · turns per merged fix: "
                   + (f"median {per['median']:.0f}, max {per['max']}" if per else "-")
                   + " · operator steps: "
                   + (f"{sum(steps.values())} (" + ", ".join(f"{name} {n}" for name, n in
                                                            steps.items()) + ")"
                      if steps else "none")
                   + (f", recorded from {_day(fixes['steps_from'])}" if fixes["steps_from"]
                      else ", none recorded yet"))
    turns, was = data["turns"] or {}, data["turns_before"]
    out.append(f"turns: {_trend(_total(turns), _total(was))} · failed "
               f"{_trend(_total(turns, 'failed'), _total(was, 'failed'))} · "
               f"held {sum(s['held'] for s in turns.values())}")
    if turns:
        out += ["", *_table(["seat", "turns", "failed", "held"],
                            [[seat, str(d["turns"]), str(d["states"].get("failed", 0)),
                              str(d["held"])] for seat, d in sorted(turns.items())])]
    if found and found["models"]:
        rows = [[name, str(g["raised"]), str(g["fixed"]), str(g["withdrawn"]),
                 str(g["overruled"]), str(g["upheld"]),
                 f"{g['false_positives']['overruled']}/{g['false_positives']['ruled']}"]
                for name, g in found["models"].items()]
        out += ["", "Findings by model", *_table(["model", "raised", "fixed", "withdrawn",
                                                 "overruled", "upheld", "false positives"],
                                                rows)]
    if reviews["models"]:
        rows = [[name, str(g["turns"]), str(g["failed"]), str(g["verdicts"].get("approved", 0)),
                 str(g["verdicts"].get("changes", 0)),
                 duration(g["to_verdict"] and g["to_verdict"]["median"]),
                 f"{g['rounds']['median']:.0f}" if g["rounds"] else "-"]
                for name, g in reviews["models"].items()]
        out += ["", "By model", *_table(["model", "turns", "failed", "approved", "changes",
                                        "to verdict (median)", "rounds (median)"], rows)]
    run = data["stuck"]
    out += ["", "most stuck: " + (f"PR #{run['pr']} ({run['seat']}) since {_day(run['since'])}: "
                                  f"{run['why']}" if run else "nothing")]
    return "\n".join(out)


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
             + ("  (a GitHub read failed: these are lower bounds)" if gh_report["partial"] else "")
             + (f"  (the window may hold more than the {GITHUB_PAGES_MAX * 100} newest PRs "
                "read: these are lower bounds)" if gh_report.get("capped") else "")]
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
    if report.get("usage"):
        out += ["", "Tokens per seat and model (counted by the inference proxy; cost only where "
                "the provider reported it)", *_table(USAGE_HEAD, _usage_rows(report))]
    for kind, title in (("prompt_rev", "prompt revision"), ("model", "model"),
                        ("thinking", "thinking")):
        groups = (report.get("revisions") or {}).get(kind)
        if groups:
            rows = [[key, str(g["turns"]),
                     ", ".join(f"{n} {v}" for v, n in sorted(g["verdicts"].items())) or "-",
                     str(g["rounds"]["max"]) if g["rounds"] else "-"]
                    for key, g in groups.items()]
            out += ["", f"By {title} (verdicts posted; rounds = most turns on one PR and seat)",
                    *_table([title, "turns", "verdicts", "rounds (max)"], rows)]
    if report.get("findings_missed") is not None:
        out += ["", f"Findings marked 'missed earlier' by reviewers in this window: {report['findings_missed']}"]
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
    if report.get("turns") is None:
        parts.append('<p class="sub">Turns: no run ledger yet.</p>')
    elif turns:
        parts.append(_html_table(TURN_HEAD, _turn_rows(report)))
    else:
        parts.append('<p class="sub">No turns in this window.</p>')
    if report.get("usage"):
        parts.append("<h2>Tokens per seat and model</h2>")
        parts.append(_html_table(USAGE_HEAD, _usage_rows(report)))
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
