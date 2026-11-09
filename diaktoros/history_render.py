"""``hermes dk history``'s text and Markdown (#1657), both from ``history.collect``'s one structure,
so the two (and ``--format json``) cannot say different things. Pure: nothing is read here."""
from __future__ import annotations

import time

from .history_summary import line as _summary

def _clock(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(at)) if at else "--:--"


def _who(event: dict) -> str:
    """``reviewer (vex · …)`` style: the seat or role, then the account that wrote, when known."""
    login = event.get("login") or ""
    return f"{event['who']} ({login})" if login else event["who"]


def _life(item: dict) -> str:
    """One finding's life, in words: raised at round N, then how it ended and where."""
    raised = f"round {item['raised']}" if item["raised"] else "an unknown round"
    tag = " ".join(part for part in (item["severity"], item["category"]) if part)
    return f"{item['id']} [{tag}] raised at {raised}, {_end(item)}"


def _end(item: dict) -> str:
    words = {"fixed": "fixed", "withdrawn": "withdrawn", "overruled": "overruled by a ruling",
             "upheld": "upheld by a ruling", "respecified": "sent back for respecification",
             "disputed": "disputed by the fixer, no ruling yet", "open": "still open"}
    end = words.get(item["outcome"], item["outcome"])
    if item["ruling"]:
        end += f" {item['ruling']}"
    if item["outcome"] != "open":
        end += f" at round {item['settled']}" if item["settled"] else " (round not on record)"
    return end


def _capital(text: str) -> str:
    """The first letter up, the rest as is: a login keeps its case (#2152)."""
    return text[:1].upper() + text[1:]


def _merge_line(data: dict) -> str:
    merged = data.get("merged")
    if merged:
        return f"merged {_clock(merged['at'] or 0)} by {merged['by'] or 'unknown'}"
    if data.get("missing"):
        return "merge state not read (--offline)"
    return f"not merged ({data.get('state') or 'state unknown'})"


def text(data: dict) -> str:
    """The timeline for a terminal: one block per round, then each finding's life."""
    lines = [f"[{data['loop']}] {data['repo']}#{data['pr']} — review history (times UTC; "
             "read-only)"]
    if _summary(data):
        lines.append(f"summary: {_summary(data)}")
    for gap in data["gaps"]:
        lines.append(f"  gap: {gap}")
    for what in data["missing"]:
        lines.append(f"  not read (--offline): {what}")
    for rnd in data["rounds"]:
        lines.append(f"round {rnd['round']} @ {rnd['head'][:7] or '(no head on record)'}")
        for e in rnd["events"]:
            line = f"  {_clock(e['at'])} {_who(e)} {e['text']}"
            lines.append(line + (f"  [gap: {e['gap']}]" if e.get("gap") else ""))
    lines.append(_merge_line(data))
    if data["findings"]:
        lines.append("findings:")
        lines += [f"  {_life(item)}" for item in data["findings"]]
    return "\n".join(lines)


def _link(data: dict, event: dict) -> str:
    """The event's link made whole: a comment anchor on the PR, a filed issue, or GitHub's own."""
    link = event.get("link") or ""
    base = f"https://github.com/{data['repo']}"
    if link.startswith("#issuecomment-"):
        return f"{base}/pull/{data['pr']}{link}"
    if link.startswith("issue:"):
        return f"{base}/issues/{link.split(':', 1)[1]}"
    return link


def _cell(value: object) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|")


def markdown(data: dict) -> str:
    """The same timeline as a document: a findings table, then a heading and a list per round,
    with each review, commit, comment and ruling linked."""
    base = f"https://github.com/{data['repo']}"
    out = [f"# Review history of [{data['repo']}#{data['pr']}]({base}/pull/{data['pr']})", "",
           f"Loop `{data['loop']}`. Times are UTC. {_capital(_merge_line(data))}.", ""]
    if _summary(data):
        out += [f"**Summary:** {_summary(data)}", ""]
    if data["gaps"] or data["missing"]:
        out += ["**Gaps on record:**", ""]
        out += [f"- {gap}" for gap in data["gaps"]]
        out += [f"- not read (`--offline`): {what}" for what in data["missing"]]
        out.append("")
    if data["findings"]:
        out += ["## Findings", "", "| ID | Severity | Category | Raised | Outcome |",
                "| --- | --- | --- | --- | --- |"]
        for item in data["findings"]:
            raised = f"round {item['raised']}" if item["raised"] else None
            out.append(f"| {item['id']} | {_cell(item['severity'])} | {_cell(item['category'])} "
                       f"| {_cell(raised)} | {_cell(_end(item))} |")
        out.append("")
    for rnd in data["rounds"]:
        head = rnd["head"]
        title = f"[`{head[:7]}`]({base}/commit/{head})" if head else "no head on record"
        out += [f"## Round {rnd['round']} @ {title}", ""]
        for e in rnd["events"]:
            link = _link(data, e)
            words = e["text"].replace("|", "\\|")
            line = f"- **{_clock(e['at'])}** {_who(e)}: {words}"
            line += f" ([link]({link}))" if link else ""
            out.append(line + (f" — *gap: {e['gap']}*" if e.get("gap") else ""))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
