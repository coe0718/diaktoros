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
            out[int(key)] = {"reason": entry["reason"][:300], "at": float(entry.get("at") or 0),
                         "label_gone": entry.get("label_gone") is True}
    return out


# The fix label is usually still on the issue when a decline is recorded (the gate read it there),
# and GitHub sends a ``labeled`` event only when a label is added: "apply it again" would send
# nothing and the decline would stay (#1833). When the label is already off the live issue (a
# delayed delivery found it gone), removing it is impossible and adding it is the event: APPLY.
REAPPLY = "remove the fix label and apply it again"
APPLY = "apply the fix label to the issue"


def label_gone_from(loop: dict, number: int) -> bool:
    """True only when the live issue, read as the reader, is readable and no longer carries the
    fix label. The gate checks pushes before reading the issue and the author before the labels,
    so the reason alone can't say; unreadable counts as "still there"."""
    from . import gh
    fix_label = str((loop.get('triage') or {}).get('fix_label') or '').casefold()
    try:
        issue = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop['read_token'])
    except Exception:  # noqa: BLE001 - a notice is not worth failing for
        return False
    if not isinstance(issue, dict) or issue.get('number') != number or not fix_label:
        return False
    present = {str((label or {}).get('name') or '').casefold()
               for label in issue.get('labels') or [] if isinstance(label, dict)}
    return fix_label not in present


def next_step(loop: dict, reason: str, label_gone: bool = False) -> str:
    """What a maintainer does about ``reason``. Every step ends by taking the label off and
    putting it back, the one act that makes GitHub deliver a new ``labeled`` event. When the
    label is already off (``label_gone``, or the reason says so) it ends by putting it on."""
    label_gone = label_gone or "no longer on the issue" in reason or "neither the fix label" in reason
    again = APPLY if label_gone else REAPPLY
    if "unattended fixer pushes" in reason:
        from . import config
        return f"turn them on with `{config.fixer_push_enable_command(loop)}`, then {again}"
    if "triage.authors" in reason:
        return (f"to allow its author, list them with `hermes dk triage --loop {loop['id']} "
                "--enable --author LOGIN` (repeat --author for every allowed author: it replaces "
                f"the list), then {again}")
    if "no longer open" in reason:
        return f"reopen the issue, then {again}"
    return f"once that is fixed, {again}"


def line(loop: dict, number: int, entry: dict) -> str:
    """The ``status`` line for one declined label."""
    return (f"issue #{number}: the fix label was declined ({entry['reason']}); "
            f"{next_step(loop, entry['reason'], entry['label_gone'])}")


def declined(loop: dict, number: int, reason: str) -> None:
    """Record the decline and notify, once per issue and reason. Best effort, never raises. The
    live label state is read once here and kept, so the notice and ``status`` give the same step."""
    from . import observer, state as state_mod
    try:
        st = state_mod.state_for(loop)
        gone = label_gone_from(loop, number)
        with st.locked():
            memory = st.kv_get(st.dir / FILE, {})
            memory = memory if isinstance(memory, dict) else {}
            memory[str(number)] = {"reason": reason, "at": time.time(), "label_gone": gone}
            st.kv_set(st.dir / FILE, memory)
        observer.notify(loop, st, "held", number, identity=f"fix-label:{reason}",
                        outcome=f"fix label declined: {reason}; {next_step(loop, reason, gone)}",
                        issue=True)
    except Exception as exc:  # noqa: BLE001 - the gate's answer is already decided
        log(f"declined fix label for #{number} not recorded: {type(exc).__name__}: {exc}")


def settle(loop: dict, st, number: int) -> None:
    """Settle the decline of ``number`` if its request is withdrawn: the live issue, read as the
    reader, is closed or no longer carries the fix label. A ``closed`` or ``unlabeled`` event is
    only the cue (review of #1804, F8): one delayed or redelivered past a newer label would
    otherwise erase that label's decline, so the live issue decides, and only the decline seen
    before the read is cleared, never one recorded while it ran. Unreadable: left. Never raises."""
    from . import gh
    seen = entries(st).get(number)
    if seen is None:
        return
    fix_label = str((loop.get('triage') or {}).get('fix_label') or '').casefold()
    try:
        issue = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop['read_token'])
    except Exception:
        return
    if not isinstance(issue, dict) or issue.get('number') != number:
        return
    present = {str((label or {}).get('name') or '').casefold()
               for label in issue.get('labels') or [] if isinstance(label, dict)}
    if issue.get('state') != 'open' or not fix_label or fix_label not in present:
        resolved(st, number, only=seen)


def reconcile(loop: dict, st) -> None:
    """The watchdog's catch-up for a missed ``closed`` or ``unlabeled`` event (review of #1804,
    F6): each declined issue is ``settle``d. One read per declined issue, as the reader (its ETag
    makes an unchanged one free); an unreadable one is left for the next sweep."""
    for number in entries(st):
        settle(loop, st, number)


def resolved(st, number: int, only: dict | None = None) -> None:
    """The request is settled (a later label handed to the fixer, the issue closed, or the fix
    label removed): the issue is no longer listed as declined. With ``only``, just that decline
    (as ``entries`` read it): a newer one recorded since is kept."""
    if number not in entries(st):
        return
    try:
        with st.locked():
            memory = st.kv_get(st.dir / FILE, {})
            current = memory.get(str(number)) if isinstance(memory, dict) else None
            if only is not None and (not isinstance(current, dict)
                                     or str(current.get('reason'))[:300] != only['reason']
                                     or float(current.get('at') or 0) != only['at']):
                return
            if isinstance(memory, dict) and memory.pop(str(number), None) is not None:
                st.kv_set(st.dir / FILE, memory)
    except Exception as exc:  # noqa: BLE001
        log(f"declined fix label for #{number} not cleared: {type(exc).__name__}: {exc}")
