"""Numbered review findings, tracked across rounds (#475).

The reviewer numbers each blocking finding in its review body, one per line::

    F1: path/to/file.py: what is wrong          (a new finding)
    F2: missed earlier: what is wrong           (new, but about code not changed this round)
    F1: fixed | open | withdrawn [reason]       (the state of an earlier finding)

From the second round on, the review must give a state to every finding still open, and a new
finding must cite a file changed in that round or say ``missed earlier`` (the stats count those
against the reviewer). The broker checks a review with ``check`` before it spends the one write
and records it with ``record`` once it landed. State lives on the host (``LoopState``).
"""
from __future__ import annotations

import re

STATES = ("fixed", "open", "withdrawn")
MISSED = "missed earlier"
_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(F\d+)\b(.*)$")
_STATUS = re.compile(r"^[\s:*\-–—]*(fixed|open|withdrawn)\b", re.IGNORECASE)


# For an id not yet known a state needs the word to be the whole line plus an optional reason:
# 'F3: open file handle leaks' and 'F4: Fixed-size buffer' are new findings, not states.
_BARE_STATUS = re.compile(r"^[\s:*\-–—]*(fixed|open|withdrawn)(?:\*\*)?(?:\s*$|\s*[:;,.(]|\s+[-–—]\s)",
                          re.IGNORECASE)


def parse(body: str, known=()) -> list[tuple[str, str, str]]:
    """``(id, kind, rest)`` per finding line; kind is a state word or ``new``.

    A line is a state when its id is in ``known``, or when the state word is the whole line
    (plus an optional reason); otherwise it is a new finding."""
    out = []
    for line in body.splitlines():
        found = _LINE.match(line)
        if found:
            status = (_STATUS if found.group(1) in known else _BARE_STATUS).match(found.group(2))
            out.append((found.group(1), status.group(1).lower() if status else "new",
                        found.group(2).strip()))
    return out


def open_ids(entry: dict) -> list[str]:
    found = entry.get("findings") if isinstance(entry, dict) else None
    found = found if isinstance(found, dict) else {}
    return sorted((k for k, v in found.items() if isinstance(v, dict) and v.get("state") == "open"),
                  key=lambda k: int(k[1:]))


def changed_files(loop: dict, number: int, prev_head: str | None, head: str) -> set[str] | None:
    """Files changed in this round (the whole PR in the first), or None when unreadable."""
    from . import gh
    if prev_head:
        data = gh.api(loop, f"/repos/{loop['repo']}/compare/{prev_head}...{head}")
        files = data.get("files") if isinstance(data, dict) else None
    else:
        files, _ = gh.pr_files_read(loop, number)
    if not isinstance(files, list):
        return None
    return {f["filename"] for f in files if isinstance(f, dict) and isinstance(f.get("filename"), str)}


def cites(rest: str, name: str) -> bool:
    """Whether ``rest`` names the path ``name`` as a whole token (``a.py`` is not in ``data.py``)."""
    return re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w-]|\.\w)", rest) is not None


def check(loop: dict, entry: dict, number: int, head: str, body: str, verdict: str = "") -> str:
    """The reason this review must be refused, or ''. Reads only; writes nothing."""
    known = entry.get("findings") if isinstance(entry.get("findings"), dict) else {}
    lines = parse(body, known)
    stated = {}
    new = []
    for fid, kind, rest in lines:
        if kind != "new" and fid in known:
            stated[fid] = kind
        elif fid in known:
            return (f"finding {fid} already exists: give it a state ({'/'.join(STATES)}) rather "
                    "than restating it; nothing was written, resubmit")
        elif fid in [n[0] for n in new]:
            return f"finding {fid} appears twice; nothing was written, resubmit"
        else:
            new.append((fid, rest))
    if verdict == "APPROVE":
        still = [f for f in open_ids(entry) if stated.get(f) not in ("fixed", "withdrawn")]
        still += [fid for fid, _ in new]
        if still:
            return (f"cannot APPROVE with open finding(s) {', '.join(sorted(set(still)))}: mark each "
                    "one fixed or withdrawn, or request changes; nothing was written, resubmit")
    later = bool(entry.get("last_head"))
    if later:
        omitted = [f for f in open_ids(entry) if f not in stated]
        if omitted:
            return (f"open finding(s) {', '.join(omitted)} left out: mark each one "
                    f"{', '.join(STATES)} (e.g. '{omitted[0]}: fixed'); nothing was written, "
                    "resubmit")
        needs = [(fid, rest) for fid, rest in new if MISSED not in rest.lower()]
        if needs:
            changed = changed_files(loop, number, entry["last_head"], head)
            if changed is None:
                return ("the host could not read what changed this round, so a new finding "
                        f"({needs[0][0]}) cannot be checked: this is a GitHub read failure, "
                        "not the finding's fault; nothing was written, retry shortly")
            for fid, rest in needs:
                if not any(cites(rest, name) for name in changed):
                    return (f"new finding {fid} cites no file changed this round: name a changed "
                            f"file in it, or mark it '{MISSED}'; nothing was written, resubmit")
    return ""


def apply(entry: dict, head: str, body: str) -> dict:
    """The entry after this review: new findings open, states updated, ``last_head`` moved."""
    entry = {**entry, "findings": {k: dict(v) for k, v in (entry.get("findings") or {}).items()}}
    later = bool(entry.get("last_head"))
    for fid, kind, rest in parse(body, entry["findings"]):
        if kind == "new":
            entry["findings"][fid] = {
                "state": "open", "text": rest[:300], "head": head,
                "missed": later and MISSED in rest.lower()}
        elif fid in entry["findings"]:
            entry["findings"][fid]["state"] = kind
    entry["last_head"] = head
    return entry


def record(loop: dict, number: int, head: str, body: str) -> None:
    from . import state as state_mod
    st = state_mod.state_for(loop)
    with st.locked():
        st.findings_put(number, apply(st.findings_get(number), head, body))


def lines(entry: dict) -> list[str]:
    found = entry.get("findings") if isinstance(entry.get("findings"), dict) else {}
    out = []
    for fid in sorted(found, key=lambda k: int(k[1:]) if k[1:].isdigit() else 0):
        item = found[fid]
        if isinstance(item, dict):
            out.append(f"{fid} {item.get('state', '?')}"
                       f"{' (missed earlier)' if item.get('missed') else ''}: "
                       f"{str(item.get('text') or '')[:120]}")
    return out


def missed_count(loop: dict) -> int:
    """Findings reviewers marked as missed earlier, across the loop's PRs."""
    from . import state as state_mod
    data = state_mod.state_for(loop).findings_all()
    return sum(1 for e in data.values() if isinstance(e, dict)
               for v in (e.get("findings") or {}).values()
               if isinstance(v, dict) and v.get("missed"))
