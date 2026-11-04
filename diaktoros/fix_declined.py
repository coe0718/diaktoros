"""A maintainer's fix label the host declined, said (#1794).

A maintainer who applies ``triage.fix_label`` is asking for a fix. When the issue can't be
handed to the fixer (its author isn't allowed, it closed, the label went, unattended fixer pushes
are off), the gate used to record the decline in ``gate-decisions.jsonl`` and nothing else. Now
the observer gets one ``held`` notice per issue and reason, naming the next step, and ``status``
lists the issue under ``held:`` until the request is resolved: a later label accepted, the issue
closed, or the label removed. Every outstanding decline is kept, in ``declined-fixes.json``: one
dropped to make room would be the silence this exists to end (review of #1804, F2).
"""
from __future__ import annotations

import json
import time

from .util import log

FILE = "declined-fixes.json"


def entries(st) -> dict[int, dict]:
    """Read-only (``status`` calls it): ``{issue: {"reason", "at"}}``. Unreadable is empty."""
    try:
        data = json.loads((st.dir / FILE).read_text())
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except Exception as exc:  # noqa: BLE001 - a status line is not worth failing for
        log(f"{FILE} is unreadable ({type(exc).__name__}: {exc}); no declined fix labels listed")
        return {}
    out = {}
    for key, entry in (data.items() if isinstance(data, dict) else ()):
        if str(key).isdigit() and isinstance(entry, dict) and isinstance(entry.get("reason"), str):
            out[int(key)] = {"reason": entry["reason"][:300], "at": float(entry.get("at") or 0)}
    return out


def next_step(loop: dict, reason: str) -> str:
    """What a maintainer does about ``reason``."""
    if "unattended fixer pushes" in reason:
        from . import config
        return f"turn them on with `{config.fixer_push_enable_command(loop)}`, then apply the label again"
    if "triage.authors" in reason:
        return (f"to allow its author, list them with `hermes dk triage --loop {loop['id']} "
                "--enable --author LOGIN` (repeat --author for every allowed author: it replaces "
                "the list), then apply the label again")
    if "no longer open" in reason:
        return "reopen the issue, then apply the label again"
    return "apply the label again once that is fixed"


def line(loop: dict, number: int, entry: dict) -> str:
    """The ``status`` line for one declined label."""
    return (f"issue #{number}: the fix label was declined ({entry['reason']}); "
            f"{next_step(loop, entry['reason'])}")


def declined(loop: dict, number: int, reason: str) -> None:
    """Record the decline and notify, once per issue and reason. Best effort, never raises."""
    from . import observer, state as state_mod
    try:
        st = state_mod.state_for(loop)
        with st.locked():
            memory = st._load(st.dir / FILE, {})
            memory = memory if isinstance(memory, dict) else {}
            memory[str(number)] = {"reason": reason, "at": time.time()}
            st._save(st.dir / FILE, memory)
        observer.notify(loop, st, "held", number, identity=f"fix-label:{reason}",
                        outcome=f"fix label declined: {reason}; {next_step(loop, reason)}",
                        issue=True)
    except Exception as exc:  # noqa: BLE001 - the gate's answer is already decided
        log(f"declined fix label for #{number} not recorded: {type(exc).__name__}: {exc}")


def resolved(st, number: int) -> None:
    """The request is settled (a later label handed to the fixer, the issue closed, or the fix
    label removed): the issue is no longer listed as declined."""
    if number not in entries(st):
        return
    try:
        with st.locked():
            memory = st._load(st.dir / FILE, {})
            if isinstance(memory, dict) and memory.pop(str(number), None) is not None:
                st._save(st.dir / FILE, memory)
    except Exception as exc:  # noqa: BLE001
        log(f"declined fix label for #{number} not cleared: {type(exc).__name__}: {exc}")
