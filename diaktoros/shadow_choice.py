"""Which reviewer turns also get a shadow review (#218), chosen by what the PR changes (#1788).

The shadow is the loop's biggest token cost, and sampling alone gives a docs-only change and a
change to the push path the same odds of a second opinion. A loop can name paths:

- ``shadow.always_paths``: a head whose PR changes one of these always gets a shadow, whatever
  ``shadow.every`` says (the write and boundary code, where a missed bug is a bad write);
- ``shadow.never_paths``: a head whose PR changes only these never gets one (docs, tests).

Any other head is sampled, one in ``shadow.every``, by repo, PR and head, as before. With neither
list set nothing is read: sampling alone, exactly as before. Globs match like ``human_paths``
(``fnmatch``, case-sensitive, ``*`` also crosses ``/``); a renamed file counts under both names,
and a path in both lists is an always path.

The changed paths are GitHub's ``pulls/N/files`` listing, read once per PR head as the reader. A
failed read shadows the head when the loop has always paths (it may change one, and
``shadow.escalate`` relies on those being shadowed: review of #1852, F1), else samples; so does
a listing GitHub cut short at 3,000 files, which may leave one out (F5). The choice is kept per PR
head in ``shadow-choices.json`` (the newest ``KEEP``), with the lists and rate it was made under:
the gate's several asks, a redelivery and ``explain`` all see the same one, and a change to the
lists or the rate makes it again (F2).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import time

from . import config
from .util import log

FILE = "shadow-choices.json"
KEEP = 500


def sampled(loop: dict, number: int, head: str) -> bool:
    """One in ``shadow.every``, by the repo, PR and head (#218): a redelivery chooses the same."""
    every = config.shadow_every(loop)
    digest = hashlib.sha256(f"{loop['repo']}:{number}:{head}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % every == 0


def _sampling(loop: dict, number: int, head: str, why: str = "") -> dict:
    every = config.shadow_every(loop)
    hit = sampled(loop, number, head)
    said = ("every head is shadowed (shadow.every 1)" if every == 1 else
            f"{'sampled' if hit else 'not sampled'}: one head in {every} (shadow.every)")
    return {"shadow": hit, "why": f"{why}; {said}" if why else said}


def policy(loop: dict) -> str:
    """What a kept choice was made under: the two lists and the rate."""
    text = json.dumps([config.shadow_always_paths(loop), config.shadow_never_paths(loop),
                       config.shadow_every(loop)])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def by_risk(loop: dict) -> bool:
    """Whether the loop chooses by paths at all (either list set)."""
    return bool(config.shadow_always_paths(loop) or config.shadow_never_paths(loop))


def choose(loop: dict, number: int, head: str) -> dict:
    """``{"shadow": bool, "why": str}`` for this PR head: the kept choice if there is one, else
    decided now and kept. Never raises: an unreadable state is a choice made again."""
    if not by_risk(loop):
        return _sampling(loop, number, head)
    key = f"{number}:{head}"
    kept = recorded(loop, number, head)
    if kept is not None:
        return kept
    choice = _decide(loop, number, head)
    made_under = policy(loop)
    try:
        from . import state as state_mod
        st = state_mod.state_for(loop)
        with st.locked():
            memory = _memory(st)
            if key in memory and memory[key]["policy"] == made_under:   # another ask was first
                return {"shadow": memory[key]["shadow"], "why": memory[key]["why"]}
            memory[key] = {**choice, "at": time.time(), "policy": made_under}
            newest = sorted(memory, key=lambda k: memory[k]["at"], reverse=True)[:KEEP]
            st._save(st.dir / FILE, {k: memory[k] for k in newest})
    except Exception as exc:  # noqa: BLE001 - the choice stands; only its record is lost
        log(f"#{number}: shadow choice at {head[:7]} not kept: {type(exc).__name__}: {exc}")
    return choice


def recorded(loop: dict, number: int, head: str) -> dict | None:
    """The choice kept for this PR head under the loop's current lists and rate, or None.
    Read-only (``explain``)."""
    try:
        from . import state as state_mod
        entry = _memory(state_mod.state_for(loop)).get(f"{number}:{head}")
    except Exception:  # noqa: BLE001 - unreadable is "none kept"
        return None
    if entry is None or entry["policy"] != policy(loop):
        return None
    return {"shadow": entry["shadow"], "why": entry["why"]}


def explain_line(loop: dict, number: int, head: str) -> str:
    """``explain``: whether this head gets a shadow review, and why; "" with no shadow profile.
    Never decides by paths itself: a choice not yet made says so."""
    if not config.shadow_profile(loop) or not head:
        return ""
    short = head[:7]
    choice = recorded(loop, number, head) if by_risk(loop) else _sampling(loop, number, head)
    if choice is None:
        return (f"no shadow choice made at {short} under the current shadow paths and rate: one "
                "is made when a reviewer turn is queued there")
    said = "gets a shadow review" if choice["shadow"] else "gets no shadow review"
    return f"head {short} {said}: {choice['why']}"


def _memory(st) -> dict:
    data = st._load(st.dir / FILE, {})
    return {key: entry for key, entry in (data.items() if isinstance(data, dict) else ())
            if isinstance(entry, dict) and isinstance(entry.get("shadow"), bool)
            and isinstance(entry.get("why"), str) and isinstance(entry.get("policy"), str)
            and isinstance(entry.get("at"), (int, float)) and not isinstance(entry["at"], bool)}


def _decide(loop: dict, number: int, head: str) -> dict:
    from . import gh
    try:
        files, error = gh.pr_files_read(loop, number)
    except Exception as exc:  # noqa: BLE001 - unread is sampled, as before
        files, error = None, f"{type(exc).__name__}: {exc}"
    if files is None:
        unread = f"its changed files could not be read ({error})"
        if config.shadow_always_paths(loop):
            return {"shadow": True, "why": f"{unread}: shadowed, since it may change a "
                                           "shadow.always_paths path"}
        return _sampling(loop, number, head, unread)
    paths = changed_paths(files)
    always = hits(paths, config.shadow_always_paths(loop))
    if always:
        more = f" and {len(always) - 1} more" if len(always) > 1 else ""
        return {"shadow": True, "why": f"it changes {always[0]}{more} (shadow.always_paths)"}
    if len(files) >= gh.PR_FILES_LISTING_LIMIT:
        # GitHub lists at most 3,000 files: an unlisted one may be an always path (review of
        # #1852, F5), and the listed ones can't prove "only never paths".
        cut = f"GitHub lists only the first {gh.PR_FILES_LISTING_LIMIT} of its changed files"
        if config.shadow_always_paths(loop):
            return {"shadow": True, "why": f"{cut}: shadowed, since an unlisted one may change a "
                                           "shadow.always_paths path"}
        return _sampling(loop, number, head, cut)
    never = config.shadow_never_paths(loop)
    if paths and len(hits(paths, never)) == len(paths):
        return {"shadow": False, "why": f"it changes only {len(paths)} path(s) matching "
                                        "shadow.never_paths"}
    return _sampling(loop, number, head)


def changed_paths(files: list) -> list[str]:
    """Every path in a ``pulls/N/files`` listing, a renamed file's old name too, without repeats."""
    out: list[str] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if isinstance(name, str) and name and name not in out:
                out.append(name)
    return out


def hits(paths: list[str], patterns: list[str]) -> list[str]:
    """The ``paths`` that match one of ``patterns``, in order."""
    return [path for path in paths if any(fnmatch.fnmatchcase(path, pat) for pat in patterns)]


def status_lines(loop: dict, limit: int = 3) -> list[str]:
    """``status``: how this loop chooses its shadow reviews, then its newest kept choices;
    [] with no shadow profile."""
    if not config.shadow_profile(loop):
        return []
    every = config.shadow_every(loop)
    rate = "every head" if every == 1 else f"one head in {every}"
    if not by_risk(loop):
        return [f"{rate}, sampled (shadow.every)"]
    always, never = config.shadow_always_paths(loop), config.shadow_never_paths(loop)
    lines = [f"by risk: always if a PR changes a shadow.always_paths glob ({len(always)}), never "
             f"if it changes only shadow.never_paths globs ({len(never)}), else {rate}, sampled"]
    try:
        from . import state as state_mod
        memory = _memory(state_mod.state_for(loop))
    except Exception:  # noqa: BLE001 - the policy line still stands
        return lines
    for key in sorted(memory, key=lambda k: memory[k]["at"], reverse=True)[:limit]:
        number, _, head = key.partition(":")
        entry = memory[key]
        lines.append(f"PR #{number} at {head[:7]}: {'shadowed' if entry['shadow'] else 'no shadow'}"
                     f" — {entry['why']}")
    return lines
