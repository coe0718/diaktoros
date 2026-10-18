"""Fixer scope lock: a fix round that strays outside its findings holds for a maintainer (#482).

With ``fixer_scope_lock`` on, a fixer push on a PR with open numbered findings (``findings``) may
change the files those findings name, plus tests and docs. Any other changed file holds the push:
nothing is written, the hold is recorded on the host with the extra files, and one ``held`` notice
lists them. It is a hold, not a refusal, because a real fix can live in a helper: ``hermes dk
retry --pr N`` records a release for that head, and the next push of that head goes through.

The check runs in ``safe_push.push`` (the broker's push path), once the changed paths are known
for both a whole-file manifest and a diff. It does not apply to a conflict-merge or CI-fix turn
(no findings to scope to), nor to a PR with no open finding, nor when ``unattended_fixer_push``
is off (no push is possible then).
"""

from __future__ import annotations

import re

from . import broker, config, findings
from .util import log

_DOC_SUFFIX = (".md", ".rst", ".txt")


class ScopeHold(broker.BrokerDenied):
    """The push changes files outside its findings and waits for a maintainer."""


def is_test_or_doc(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (bool({"tests", "test", "docs", "doc"} & set(parts[:-1]))
            or name.startswith("test_") or re.search(r"_test\.\w+$", name) is not None
            or name.endswith(_DOC_SUFFIX))


def extras(entry: dict, paths: list[str]) -> list[str]:
    """The changed ``paths`` no open finding names and that are not tests or docs; sorted.
    ``[]`` when there is no open finding (nothing to scope to)."""
    texts = [str((entry.get("findings") or {}).get(fid, {}).get("text") or "")
             for fid in findings.open_ids(entry)]
    if not texts:
        return []
    return sorted({p for p in paths
                   if not is_test_or_doc(p) and not any(findings.cites(t, p) for t in texts)})


def check(loop: dict, number: int, head: str, paths: list[str]) -> None:
    """Raise ``ScopeHold`` when this push leaves its findings' files and is not released."""
    if not config.fixer_scope_lock(loop):
        return
    from . import state as state_mod
    st = state_mod.state_for(loop)
    out = extras(st.findings_get(number), list(paths))
    if not out:
        return
    held = st.scope_holds().get(number) or {}
    if (held.get("head") == head and held.get("released") is True
            and set(out) <= set(held.get("files") or [])):    # only the files it was shown
        log(f"#{number}: scope lock released by a maintainer for {head[:7]}; push goes through")
        return
    record = {"head": head, "files": out, "released": False}
    if st.scope_hold_set(number, record):
        _notice(loop, st, number, head, out)
    raise ScopeHold(
        f"held for a maintainer: this fix changes {len(out)} file(s) outside its findings "
        f"({', '.join(out[:10])}); nothing was written. A maintainer releases it with "
        f"`hermes dk retry --loop {loop.get('id')} --pr {number}`")


def snapshot(loop: dict, number: int) -> dict | None:
    """The exact hold a retry may release later, or no approval when none exists."""
    if not config.fixer_scope_lock(loop):
        return None
    from . import state as state_mod
    return state_mod.state_for(loop).scope_hold_snapshot(number)


_CURRENT_HOLD = object()


def release(loop: dict, number: int, *, expected=_CURRENT_HOLD) -> dict | None:
    """Release only ``expected`` if unchanged, returning the released hold or ``None``.

    An explicit ``None`` means no hold was approved, NOT permission to release a new hold.
    Direct callers may omit the snapshot to approve the current hold; even then the compare
    and write are atomic, so a concurrent replacement can never be overwritten or approved.
    With the lock off there is no state to open.
    """
    if not config.fixer_scope_lock(loop):
        return None
    from . import state as state_mod
    st = state_mod.state_for(loop)
    if expected is _CURRENT_HOLD:
        expected = st.scope_hold_snapshot(number)
    assert expected is None or isinstance(expected, dict)
    return st.scope_hold_release(number, expected)


def _notice(loop: dict, st, number: int, head: str, out: list[str]) -> None:
    from . import observer
    try:
        observer.notify(loop, st, "held", number, head, identity=f"scope:{head}",
                        outcome=(f"fix held: it changes files outside its findings: "
                                 f"{', '.join(out[:10])}. `hermes dk retry --loop {loop.get('id')} "
                                 f"--pr {number}` lets it through")[:400])
    except Exception as exc:                      # a notice is best effort, never the gate
        log(f"#{number}: scope-lock notice not sent: {exc}")


def line(number: int, hold: dict) -> str:
    """One status line for a hold (``status`` prints it)."""
    state = "released" if hold.get("released") else "held"
    return (f"PR #{number} fix {state} by the scope lock: "
            f"{', '.join(hold.get('files') or [])} (`hermes dk retry --pr {number}` releases)")
