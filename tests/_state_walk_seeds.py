"""The seeds ``tests/_state_walk.py`` lays beside a file (#2589 review F8): a file read only when
other files hold particular valid values, and not others, is reached whichever values those are,
together: every combination of each companion's valid values, or a failure when there are too
many to try (closed, never a silent pass)."""
from __future__ import annotations

import itertools
import math

COMBINATIONS = 256      # most companion combinations tried for one call before failing closed


def alternative(alts: dict, file: str, value) -> None:
    """Keep ``value``, one a call wrote into ``file``, among ``file``'s valid alternatives: each
    distinct whole value, never folded into another, so two writers' conflicting values (one
    ``{"ready": false}``, one ``{"ready": true}``) are both kept."""
    have = alts.setdefault(file, [])
    if value not in have:
        have.append(value)


def variants(seed: dict, alts: dict, cap: int | None = None) -> list[dict] | None:
    """Every combination of the files in ``seed`` (file -> its folded sample) each holding its
    folded sample or one of its valid alternatives, the folded seed first: so a branch that opens
    only when two files hold particular values at once is reached. Empty when ``seed`` is; None
    when there would be more than ``cap``."""
    if not seed:
        return []
    files, options = sorted(seed), []
    for file in files:
        values, keys = [seed[file]], {repr(seed[file])}
        for value in alts.get(file, ()):
            if (key := repr(value)) not in keys:
                keys.add(key)
                values.append(value)
        options.append(values)
    if math.prod(map(len, options)) > (COMBINATIONS if cap is None else cap):
        return None
    return [dict(zip(files, combo)) for combo in itertools.product(*options)]


def seeds(seed: dict, alts: dict, uncovered: set, label: str) -> list[dict]:
    """``variants(seed, alts)``; past the cap, the folded seed alone, with ``label`` and why
    kept in ``uncovered`` for the walk to fail on."""
    found = variants(seed, alts)
    if found is not None:
        return found
    uncovered.add(f"{label}: more than {COMBINATIONS} combinations of its companions' valid "
                  "values, so not every one was laid beside it (fails closed)")
    return [seed]
