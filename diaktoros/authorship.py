"""Who wrote a commit on a loop PR, against who the loop says pushed it (#757).

The broker refuses a fixer push from the wrong identity (#233). This is the other half: a commit
on a loop PR whose author GitHub maps to a seat's account (``config.seat_accounts``), but which no
seat turn pushed, is **misattributed** — the PR history says the seat wrote code it did not (for
example an agent that committed with the fixer's git email). A seat turn's pushes are the heads the
broker's audit journal (``broker-audit.jsonl``) recorded; a commit that is none of them is not
from a seat turn.

Report-only: ``explain`` and ``doctor`` print it; nothing here blocks a turn, a push or a merge.
It cannot fire for a commit whose author email GitHub does not map to an account (``author`` is
null), and it only knows pushes made since the audit journal began — a journal that was removed
makes every seat-attributed commit on a PR look unpushed.
"""
from __future__ import annotations

import json

from . import config, gh

# Open PRs one doctor run reads the commits of (one read per PR): bounded, and said when cut.
PRS_MAX = 30


def pushed_heads(loop: dict) -> tuple[set[str], str]:
    """``(shas, error)``: every commit a seat turn of this loop pushed or tried to push, from the
    broker's audit journal. A missing journal is no pushes; an unreadable one is an error."""
    path = config.state_dir(loop) / "broker-audit.jsonl"
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return set(), ""
    except (OSError, UnicodeDecodeError) as exc:
        return set(), f"{path}: {type(exc).__name__}: {exc}"
    found: set[str] = set()
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue                    # a torn line is not a push record
        if isinstance(entry, dict) and entry.get("repo") == loop["repo"]:
            # Only ``new_head``, the commit a push made: ``head`` is the PR head a write was
            # about (a review of a stray commit must not make it look pushed).
            if isinstance(entry.get("new_head"), str) and entry["new_head"]:
                found.add(entry["new_head"])
    return found, ""


def misattributed(loop: dict, commits: list, pushed: set[str]) -> list[dict]:
    """The commits among ``commits`` (GitHub's PR commit list) authored by a seat's account that
    no seat turn pushed: ``[{sha, login, subject}]``. Pure."""
    seats = config.seat_accounts(loop)
    out = []
    for item in commits:
        if not isinstance(item, dict):
            continue
        account = item.get("author")
        login = str(account.get("login") or "").lower() if isinstance(account, dict) else ""
        sha = str(item.get("sha") or "")
        if login and login in seats and sha and sha not in pushed:
            message = str((item.get("commit") or {}).get("message") or "")
            out.append({"sha": sha, "login": login, "subject": message.split("\n", 1)[0][:80]})
    return out


def scan(loop: dict, number: int, pushed: set[str] | None = None) -> tuple[list[dict], str]:
    """``(misattributed commits, error)`` for one PR. An unread commit list or journal is an
    error, never "none"."""
    if pushed is None:
        pushed, error = pushed_heads(loop)
        if error:
            return [], f"push journal unreadable ({error})"
    commits, error = gh.pr_commits_read(loop, number)
    if commits is None:
        return [], f"commits unreadable ({error})"
    return misattributed(loop, commits, pushed), ""


def line(entry: dict) -> str:
    """The one sentence ``explain`` and ``doctor`` use for a misattributed commit."""
    subject = f" \"{entry['subject']}\"" if entry.get("subject") else ""
    return (f"commit {entry['sha'][:7]}{subject} shows as {entry['login']}'s, but no seat turn "
            f"pushed it — its git author email maps to that account (a stray git identity?)")
