#!/usr/bin/env python3
"""Triage gate (#213) — decides whether a new issue is queued for the isolated triage turn.

Route this at the triage profile, on the ``issues`` event. Only ``opened`` counts, and only for
an issue whose author is in the loop's ``triage.authors``: anyone else's issue is dropped here,
before any model sees its text (spam and prompt injection on a public repo stop at the gate).
The issue is re-read from GitHub first; one that is closed, is a pull request, changed author,
or already carries a label from the triage list (a person got there first) is left alone.

stdin : a GitHub webhook payload
stdout: ``[SILENT]`` (an eligible issue queues an isolated triage turn)
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from review_loop import config, gate, gh  # noqa: E402
from review_loop.util import log, silence  # noqa: E402


def main() -> None:
    payload = json.load(sys.stdin)
    if "zen" in payload and "action" not in payload:
        silence("hook ping")
    loop, _st = gate.context(payload)
    triage = loop.get("triage") or {}
    if not triage.get("route"):
        silence("issue triage is off for this loop")
    if payload.get("action") != "opened":
        silence(f"issues {payload.get('action') or '(no action)'}: only a new issue is triaged")
    issue = payload.get("issue")
    if not isinstance(issue, dict) or type(issue.get("number")) is not int or issue["number"] < 1:
        silence("payload has no issue number")
    number = issue["number"]
    author = str((issue.get("user") or {}).get("login") or "").lower()
    if author not in triage["authors"]:
        silence(f"issue #{number} by {author or 'an unknown author'}: not in triage.authors")
    live = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop["read_token"])
    if not isinstance(live, dict) or live.get("number") != number:
        silence(f"issue #{number} unreadable (GitHub read failed)")
    if "pull_request" in live:
        silence(f"#{number} is a pull request, not an issue")
    if live.get("state") != "open":
        silence(f"issue #{number} is not open")
    if str((live.get("user") or {}).get("login") or "").lower() != author:
        silence(f"issue #{number} author changed")
    present = {str((label or {}).get("name") or "").casefold() for label in live.get("labels") or []
               if isinstance(label, dict)}
    if present & {name.casefold() for name in triage["labels"]}:
        silence(f"issue #{number} already carries a triage label: a person labelled it")
    try:
        outcome = gate.enqueue_isolated(loop, "triage", number, config.TRIAGE_HEAD,
                                        turn_key="triage")
    except Exception as exc:
        silence(f"issue #{number} triage held: isolated worker unavailable: "
                f"{type(exc).__name__}: {exc}")
    log(f"issue #{number} triage {outcome}")
    silence()


if __name__ == "__main__":
    from review_loop import gate_failures  # noqa: E402
    gate_failures.run("gate_triage", main)
