"""The seeds ``tests/_state_walk.py`` lays beside a file (#2589 review F8): a file read only when
another holds one valid value, and not another, is reached whichever value that is."""
from __future__ import annotations


def alternative(alts: dict, file: str, value) -> None:
    """Keep ``value``, one a call wrote into ``file``, among ``file``'s valid alternatives: each
    distinct whole value, never folded into another, so two writers' conflicting values (one
    ``{"ready": false}``, one ``{"ready": true}``) are both kept."""
    have = alts.setdefault(file, [])
    if value not in have:
        have.append(value)


def variants(seed: dict, alts: dict) -> list[dict]:
    """``seed`` (file -> its folded sample), then ``seed`` with one file at a time holding each
    of its other valid alternatives instead: every value a method wrote there is laid beside the
    rest at least once. Empty when ``seed`` is."""
    if not seed:
        return []
    out, keys = [seed], {repr(seed)}
    for file in sorted(seed):
        for value in alts.get(file, ()):
            variant = {**seed, file: value}
            if (key := repr(variant)) not in keys:
                keys.add(key)
                out.append(variant)
    return out
