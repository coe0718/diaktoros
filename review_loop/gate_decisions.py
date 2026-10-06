"""A small, durable record of what each gate decided (#209).

Hermes drops a script's stderr when it exits 0, and every gate answers ``[SILENT]`` with exit 0,
so the reason a gate declined was lost. ``silence(reason)`` now also appends one line here:
``<state dir>/gate-decisions.jsonl``, written under its own ``flock`` and pruned to the last
``KEEP`` lines. Best effort: nothing in here may change the gate's answer. No payload body is
ever stored, only gate, event, action, PR/issue number, head (7), sender, decision and reason.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile

from . import config, util

KEEP = 200
LIMIT = 300
FILE = "gate-decisions.jsonl"


def _clip(value, limit: int = LIMIT) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _entry(gate: str, payload: dict, decision: str, reason: str) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    obj = next((payload[k] for k in ("pull_request", "issue") if isinstance(payload.get(k), dict)), {})
    number = obj.get("number") or payload.get("number")
    head = (obj.get("head") or {}).get("sha") if isinstance(obj.get("head"), dict) else ""
    sender = (payload.get("sender") or {}).get("login") if isinstance(payload.get("sender"), dict) else ""
    event = ("pull_request_review" if isinstance(payload.get("review"), dict)
             else "issues" if isinstance(payload.get("issue"), dict) else "pull_request")
    return {"time": util.now_iso(), "gate": _clip(gate, 40), "event": event,
            "action": _clip(payload.get("action"), 40),
            "number": number if type(number) is int else None,
            "head": _clip(head, 7), "sender": _clip(sender, 60),
            "decision": _clip(decision, 40), "reason": _clip(reason)}


def record(loop: dict, gate: str, payload: dict, decision: str, reason: str) -> None:
    """Append one decision. Never raises."""
    try:
        directory = config.state_dir(loop)
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_entry(gate, payload, decision, reason), sort_keys=True) + "\n"
        fd = os.open(directory / "gate-decisions.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = directory / FILE
            with open(path, "a", encoding="utf-8") as out:
                out.write(line)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(lines) > KEEP:
                handle, name = tempfile.mkstemp(prefix=".gate-decisions-", dir=directory)
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    out.writelines(lines[-KEEP:])
                os.replace(name, path)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 - best effort by design
        try:
            print(f"[review-loop] gate decision not recorded: {type(exc).__name__}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


def read(loop: dict) -> list[dict]:
    """Every readable entry, oldest first. Read-only; unreadable is empty."""
    try:
        text = (config.state_dir(loop) / FILE).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for raw in text.splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def for_pr(loop: dict, number: int, limit: int = 5) -> list[dict]:
    return [e for e in read(loop) if e.get("number") == number][-limit:]


def latest_per_gate(loop: dict) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for entry in read(loop):
        latest[str(entry.get("gate"))] = entry
    return latest


def line(entry: dict) -> str:
    what = f"{entry.get('event')}/{entry.get('action')}" if entry.get("action") else str(entry.get("event"))
    who = f" by {entry['sender']}" if entry.get("sender") else ""
    head = f" @ {entry['head']}" if entry.get("head") else ""
    num = f" #{entry['number']}" if entry.get("number") else ""
    return (f"{entry.get('time')} {entry.get('gate')} {what}{num}{head}{who}: "
            f"{entry.get('decision')} — {entry.get('reason')}")
