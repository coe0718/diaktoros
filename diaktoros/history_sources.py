"""The state-dir and GitHub halves of ``hermes dk history`` (#1657): the broker's write journal,
the finding record, and the PR's reviews, commits and comments. Read-only; an unreadable store or
a failed read is added to ``gaps``, never guessed around."""
from __future__ import annotations

import json

from . import config, gh, stats

EXCERPT = 240                                   # characters of one free text a line shows
# Writes the ledger already shows as its own event (a verdict, answers, a ruling, a filed issue):
# kept for telling a turn that wrote from one that did not, left out of the printed timeline.
_LEDGER_WRITES = {"review", "answers", "ruling_comment", "file_issue", "file_shadow_issue",
                  "ruled_approval", "ruled_dismissal"}


def excerpt(text: object) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= EXCERPT else flat[:EXCERPT - 1] + "…"


def audit_writes(loop: dict, number: int, gaps: list) -> list:
    """Every write the broker journaled for this PR; a push shows the commit it made."""
    path = config.state_dir(loop) / "broker-audit.jsonl"
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        gaps.append(f"broker-audit.jsonl is unreadable ({type(exc).__name__}): no write records")
        return []
    out, pushes = [], {}
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue                                 # a torn line is not a write record
        if not isinstance(entry, dict) or entry.get("repo") != loop["repo"] \
                or entry.get("pr") != number:
            continue
        event = {"at": float(entry.get("at") or 0), "kind": "write", "link": "",
                 "head": str(entry.get("old_head") or entry.get("head") or ""),
                 "who": str(entry.get("role") or "?"), "login": str(entry.get("login") or ""),
                 "operation": str(entry.get("operation") or "?")}
        new = entry.get("new_head")
        if isinstance(new, str) and new:
            outcome = entry.get("outcome")
            event.update(new_head=new, text=f"pushed {new[:7]}"
                         + (f" ({outcome})" if outcome and outcome != "pushed" else ""))
            pushes[new] = event                      # the reconciled record replaces the attempt
            continue
        event["text"] = event["operation"].replace("_", " ")
        event["shown"] = event["operation"] not in _LEDGER_WRITES
        out.append(event)
    return out + list(pushes.values())


def tracked_findings(loop: dict, number: int, gaps: list) -> dict:
    path = config.state_dir(loop) / "findings.json"
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        gaps.append(f"findings.json is unreadable ({type(exc).__name__}): finding lives not shown")
        return {}
    entry = raw.get(str(number)) if isinstance(raw, dict) else None
    found = entry.get("findings") if isinstance(entry, dict) else None
    return found if isinstance(found, dict) else {}


def github_reads(loop: dict, number: int, gaps: list) -> dict:
    """The PR, its reviews, commits and comments, read once each; a failed read is a gap."""
    out = {}
    pr, error = gh.fetch(loop, gh.pr_path(loop, number))
    if isinstance(pr, dict):
        out["pr"] = pr
    else:
        gaps.append(f"GitHub: the PR could not be read ({error or 'no data'})")
    for key, read in (("reviews", gh.reviews_read), ("commits", gh.pr_commits_read),
                      ("comments", gh.issue_comments_read)):
        items, error = read(loop, number)
        if isinstance(items, list):
            out[key] = items
        else:
            gaps.append(f"GitHub: the PR's {key} could not be read ({error or 'no data'})")
    return out


def github_events(loop: dict, number: int, github: dict, events: list) -> list:
    """Review bodies join their receipt; a review, commit or comment without one is its own."""
    receipts = {e["review_id"]: e for e in events if e["kind"] == "review" and e["review_id"]}
    pushed = {e["new_head"]: e for e in events if e.get("new_head")}
    out = []
    for review in github.get("reviews") or []:
        body, link = str(review.get("body") or ""), str(review.get("html_url") or "")
        mine = receipts.get(review.get("id"))
        if mine is not None:
            mine.update(text=f"{mine['text']} — {excerpt(body)}", link=link,
                        login=str((review.get("user") or {}).get("login") or ""))
            continue
        out.append({"at": stats._iso(review.get("submitted_at")) or 0.0, "kind": "github-review",
                    "head": str(review.get("commit_id") or ""), "link": link,
                    "who": str((review.get("user") or {}).get("login") or "?"),
                    "text": f"{gh.review_state(review)} — {excerpt(body)}"})
    for item in github.get("commits") or []:
        sha = str(item.get("sha") or "")
        subject = str((item.get("commit") or {}).get("message") or "").split("\n", 1)[0]
        if sha in pushed:
            pushed[sha]["text"] += f": {excerpt(subject)}"
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        when = ((item.get("commit") or {}).get("committer") or {}).get("date")
        out.append({"at": stats._iso(when) or 0.0, "kind": "commit", "head": "",
                    "who": str(author.get("login") or "?"), "link": str(item.get("html_url") or ""),
                    "text": f"commit {sha[:7]}: {excerpt(subject)}"})
    for item in github.get("comments") or []:
        out.append({"at": stats._iso(item.get("created_at")) or 0.0, "kind": "comment",
                    "head": "", "who": str((item.get("user") or {}).get("login") or "?"),
                    "link": str(item.get("html_url") or ""),
                    "text": f"comment: {excerpt(item.get('body'))}"})
    return out
