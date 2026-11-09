"""``hermes dk history --pr N``: one PR's whole review cycle as a timeline (#1657). Read-only.

Everything a cycle leaves is already recorded, spread over three stores: the run ledger (turns,
receipts, answers, disputes, rulings, shadow reviews, filed issues, ...), the loop's state dir
(``findings.json``, ``broker-audit.jsonl``) and GitHub (review bodies, commits, comments). This
joins them into one structure, rounds grouped by head, that ``history_render`` prints as text or
Markdown and ``--format json`` prints as is. It writes nothing, infers nothing: an unreadable
store or a turn with no write on record is shown as a gap. Every free text passes ``redact``.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import findings as findings_mod, stats
from .history_sources import audit_writes, excerpt, github_reads, github_events, tracked_findings
from .run_supervisor import NOT_ENDED, views

# What each ledger table adds to the timeline: its event kind and the columns it is read with.
# Rows are joined to their run (seat, head) by run_id; a table without run_id carries its own head.
_TABLES = {
    "review_receipts": ("review", "t.verdict, t.review_id, COALESCE(t.confirmed, t.created) AS at"),
    "fixer_answers": ("answers", "t.body, t.state, t.comment_id, t.created AS at"),
    "disputes": ("dispute", "t.body, t.created AS at"),
    "declines": ("decline", "t.kind AS decline_kind, t.body, t.created AS at"),
    "rulings": ("ruling", "t.verdict, t.turn_key, t.body, t.comment_id, t.created AS at"),
    "shadow_reviews": ("shadow", "t.model AS shadow_model, t.verdict, t.body, t.created AS at"),
    "filed_issues": ("filed", "t.title, t.state, t.issue_number, t.created AS at"),
    "shadow_issues": ("shadow-filed", "t.title, t.state, t.issue_number, t.created AS at"),
}
_OWN_HEAD = {
    "ruled_approvals": ("ruled-approval", "head, ruling, approve, review_id, dismiss, created AS at"),
    "overrides": ("override", "head, kind AS override_kind, findings, merged_at AS at"),
    "operator_actions": ("operator", "'' AS head, action, created AS at"),
}
# The JSON's shape (``--format json``): raised when a key is renamed, removed or changes meaning,
# not when one is added. Tools such as a published run viewer check it. Gaps name a store, never
# a local path.
SCHEMA = 1
_RUN_COLUMNS = ("id", "seat", "head", "state", "created", "launch_intent", "finished", "model",
                "tokens_in", "tokens_out", "cost", "turn_key", "detail", "error")
# An absolute local path or a file:// URL in a free text (a failed turn's output tail, a review
# body): shown as its last part only, so no output names a host's layout (#2134). A part may hold
# spaces, so a run is cut at its last ``/`` even across words (#2151): a few words before it may
# go too, a directory name never shows. An http(s) URL's path is a link, not a local path.
_PART = r"[\w.~%-]+(?: +(?!(?i:file)://)[\w.~%-]+)*"         # a file:// URL starts its own
_PATH = re.compile(rf"(?i:\bfile://)[^\s/]*(?:/{_PART})+"
                   rf"|(?<![\w.:/~%-])(?:~|/)(?:{_PART})?(?:/{_PART})+")
_SEVERITY = re.compile(r"\b(P[0-4])\b")


def collect(loop: dict, db: Path, number: int, offline: bool, redact) -> dict:
    """The PR's cycle: ``{repo, pr, rounds, findings, merged, gaps, missing}``. ``rounds`` is a
    list of ``{head, events}`` in the order each head was first seen; an event is a dict with
    ``at``, ``kind``, ``who``, ``text`` and, when known, ``link`` and ``gap``."""
    data = {"schema": SCHEMA, "repo": loop["repo"], "loop": loop["id"], "pr": number,
            "gaps": [], "missing": []}
    events, runs, rulings, disputes = _ledger(db, loop["repo"], number, data["gaps"])
    events += audit_writes(loop, number, data["gaps"])
    tracked = tracked_findings(loop, number, data["gaps"])
    github = {} if offline else github_reads(loop, number, data["gaps"])
    if offline:
        data["missing"] = ["review bodies (findings text, per-round finding states)",
                           "the PR's commits", "the PR's comments", "whether it merged"]
    events += github_events(loop, number, github, events)
    _mark_writeless(runs, events)
    events += [_turn_event(run) for run in runs]
    for event in events:
        event["text"] = _PATH.sub(_path_tail, redact(event.get("text") or ""))
    data["rounds"] = _rounds([e for e in events if e.get("shown", True)])
    data["findings"] = _findings(tracked, number, data["rounds"], github.get("reviews") or [],
                                 rulings, disputes)
    pr = github.get("pr") or {}
    data["merged"] = ({"at": stats._iso(pr.get("merged_at")),
                       "by": str((pr.get("merged_by") or {}).get("login") or "")}
                      if pr.get("merged_at") else None)
    data["state"] = "merged" if data["merged"] else str(pr.get("state") or "")
    return data


def _ledger(db: Path, repo: str, number: int, gaps: list) -> tuple:
    """``(events, runs, rulings, disputes)`` from the run ledger, opened read-only."""
    try:
        con = views.read_only_connect(db)
    except sqlite3.Error as exc:
        gaps.append(f"the run ledger is unreadable ({type(exc).__name__}): no turns, verdicts or "
                    "rulings")
        return [], [], [], []
    if con is None:
        gaps.append("no run ledger: no turns, verdicts or rulings")
        return [], [], [], []
    try:
        have = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        cols = ", ".join(c if c in have else f"NULL AS {c}" for c in _RUN_COLUMNS)
        runs = [dict(r) for r in con.execute(f"SELECT {cols} FROM runs WHERE repo=? AND pr=? "
                                             "ORDER BY created", (repo, number))]
        events = []
        for table, (kind, columns) in _TABLES.items():
            events += _rows(con, gaps, table, kind, f"SELECT r.seat, r.head, r.id AS run, {columns} "
                            f"FROM {table} t JOIN runs r ON r.id = t.run_id "
                            "WHERE r.repo=? AND r.pr=?", (repo, number))
        for table, (kind, columns) in _OWN_HEAD.items():
            events += _rows(con, gaps, table, kind, f"SELECT {columns} FROM {table} "
                            "WHERE repo=? AND pr=?", (repo, number))
        events += _rows(con, gaps, "issue_fixes", "issue-fix",
                        "SELECT number AS issue, base AS head, created AS at FROM issue_fixes "
                        "WHERE repo=? AND pr_number=?", (repo, number))
    finally:
        con.close()
    rulings = [{"pr": number, **e} for e in events if e["kind"] == "ruling"]
    disputes = [{"pr": number, "created": e["at"]} for e in events if e["kind"] == "dispute"]
    for e in rulings:
        e["created"] = e["at"]
    rulings.sort(key=lambda e: e["at"])
    return [_describe(e) for e in events], runs, rulings, disputes


def _rows(con, gaps: list, table: str, kind: str, sql: str, params: tuple) -> list:
    try:
        return [{**dict(row), "kind": kind} for row in con.execute(sql, params)]
    except sqlite3.Error as exc:
        gaps.append(f"ledger table {table} is unreadable ({exc}): its records are not shown")
        return []


def _describe(e: dict) -> dict:
    """A ledger row as a timeline event: its words, from that row alone."""
    kind, text = e["kind"], ""
    if kind == "review":
        text = f"{e['verdict'] or 'verdict unknown'} (review {e['review_id'] or '?'})"
    elif kind == "answers":
        text = f"answers ({e['state']}): {excerpt(e['body'])}"
    elif kind == "dispute":
        text = f"dispute: {excerpt(e['body'])}"
    elif kind == "decline":
        text = f"decline ({e['decline_kind']}): {excerpt(e['body'])}"
    elif kind == "ruling":
        text = f"ruling {e['verdict']} ({e['turn_key']}) — \"{excerpt(e['body'])}\""
    elif kind == "shadow":
        text = f"{e['verdict']} ({e['shadow_model'] or 'model unknown'}) — {excerpt(e['body'])}"
    elif kind in ("filed", "shadow-filed"):
        number = f"#{e['issue_number']}" if e["issue_number"] else f"({e['state']})"
        text = f"filed {number}: {excerpt(e['title'])}"
    elif kind == "ruled-approval":
        text = f"ruled approval: approve {e['approve']}, dismiss {e['dismiss']}"
    elif kind == "override":
        text = f"{e['override_kind'].replace('_', ' ')} (open findings: {excerpt(e['findings'])})"
    elif kind == "operator":
        text = f"operator {e['action']}"
    elif kind == "issue-fix":
        text = f"opened from issue #{e['issue']}"
    who = e.get("seat") or ("adjudicator" if kind == "ruled-approval" else kind)
    return {"at": float(e.get("at") or 0), "head": str(e.get("head") or ""), "kind": kind,
            "who": who, "text": text, "run": e.get("run"), "link": _ledger_link(e),
            "review_id": e.get("review_id") if kind == "review" else None}


def _ledger_link(e: dict) -> str:
    if e.get("comment_id"):
        return f"#issuecomment-{e['comment_id']}"            # made whole by the renderer
    if e["kind"] in ("filed", "shadow-filed") and e.get("issue_number"):
        return f"issue:{e['issue_number']}"
    return ""


def _mark_writeless(runs: list, events: list) -> None:
    """A turn that ended with no write on record (no ledger row of its own, no journaled write
    by its seat at its head while it ran) is shown as a gap, never as a step that happened."""
    owned = {e.get("run") for e in events if e.get("run")}
    writes = [e for e in events if e["kind"] == "write"]
    for run in runs:
        if run["state"] in NOT_ENDED or run["state"] in ("blocked", "uncertain"):
            continue                 # not ended (#2133); uncertain says its own outcome below
        end = (run["finished"] or run["created"]) + 120
        run["no_write"] = run["id"] not in owned and not any(
            w["who"] == run["seat"] and w["head"] == run["head"]
            and run["created"] <= w["at"] <= end for w in writes)


def _path_tail(found: re.Match) -> str:
    return "…/" + found.group(0).rstrip("/").rsplit("/", 1)[-1]


def _turn_event(run: dict) -> dict:
    parts = [run["model"] or "model unknown"]
    if run["tokens_in"] is not None or run["tokens_out"] is not None:
        tokens = (run["tokens_in"] or 0) + (run["tokens_out"] or 0)
        parts.append(f"{tokens / 1000:.0f}k tok" if tokens >= 1000 else f"{tokens} tok")
    if run["cost"] is not None:
        parts.append(f"${run['cost']:.2f}")
    text = f"turn {run['state']} ({' · '.join(parts)})"
    if run["turn_key"]:
        text += f" [{run['turn_key']}]"
    if run["state"] == "uncertain":
        text += " — its write's outcome is unknown"
    if run["state"] not in ("succeeded",) and (run["error"] or run["detail"]):
        text += f" — {excerpt(run['error'] or run['detail'])}"
    return {"at": float(run["launch_intent"] or run["created"]), "kind": "turn",
            "head": run["head"], "who": run["seat"], "text": text, "run": run["id"], "link": "",
            "gap": "ended without a write on record" if run.get("no_write") else ""}


def _rounds(events: list) -> list:
    """Rounds by head, in first-seen order; an event without a head joins the round of the
    latest head first seen at or before it (the first round when it predates them all)."""
    first: dict[str, float] = {}
    for e in sorted(events, key=lambda e: e["at"]):
        if e["head"] and e["head"] not in first:
            first[e["head"]] = e["at"]
    heads = list(first) or [""]
    rounds = {head: [] for head in heads}
    for e in events:
        head = e["head"] if e["head"] in rounds else next(
            (h for h in reversed(heads) if first.get(h, 0) <= e["at"]), heads[0])
        rounds[head].append(e)
    return [{"round": i, "head": head, "events": sorted(rounds[head], key=lambda e: e["at"])}
            for i, head in enumerate(heads, 1)]


def _findings(tracked: dict, number: int, rounds: list, reviews: list,
              rulings: list, disputes: list) -> list:
    """Each finding's one-line life: raised at round N, then its outcome and, when a review body
    or a ruling shows it, the round that settled it. Outcomes are ``stats.finding_outcome``'s."""
    at_round = {r["head"]: r["round"] for r in rounds}
    settled, known = {}, set()
    for review in sorted(reviews, key=lambda r: str(r.get("submitted_at") or "")):
        for fid, kind, _ in findings_mod.parse(str(review.get("body") or ""), known):
            known.add(fid)
            if kind in ("fixed", "withdrawn"):
                settled.setdefault(fid, at_round.get(str(review.get("commit_id") or "")))
    out = []
    for fid, item in sorted(tracked.items(), key=lambda kv: _fnum(kv[0])):
        if not isinstance(item, dict):
            continue
        ruling = stats.deciding_ruling(item, number, rulings)
        severity = _SEVERITY.search(str(item.get("text") or ""))
        out.append({"id": fid, "severity": severity.group(1) if severity else "",
                    "category": str(item.get("category") or "other"),
                    "raised": at_round.get(str(item.get("head") or "")),
                    "outcome": stats.finding_outcome(item, number, rulings, disputes),
                    "settled": settled.get(fid) if not ruling else at_round.get(ruling["head"]),
                    "ruling": f"{ruling['verdict']} ({ruling['turn_key']})" if ruling else ""})
    return out


def _fnum(fid: str) -> int:
    return int(fid[1:]) if fid[1:].isdigit() else 0
