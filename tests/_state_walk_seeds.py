"""The seeds ``tests/_state_walk.py`` lays beside a file (#2589 review F8): a file read only when
other files hold particular valid values, and not others, is reached whichever values those are,
together: every combination of each companion's valid values, or a failure when there are too
many to try (closed, never a silent pass)."""
from __future__ import annotations

import itertools
import math

from _state_walk_oracle import kind

COMBINATIONS = 256      # most companion combinations tried for one call before failing closed
_NONE = object()


def fold(samples: dict, alts: dict, file: str, value) -> bool:
    """Fold ``value``, one a call wrote into ``file``, into the file's valid sample, and keep it
    whole among its alternatives: each distinct value, never folded into another, so two
    writers' conflicting values (``{"ready": false}``, ``{"ready": true}``) are both kept.
    Whether either gained something, so discovery calls again each method that touches the file
    (round 10: a new whole value that leaves the folded sample as it was counts too)."""
    have, kept = samples.get(file, _NONE), alts.setdefault(file, {})
    added = (key := repr(skeleton(value))) not in kept
    if added:
        kept[key] = value
    if isinstance(have, dict) and isinstance(value, dict):
        new = {**value, **have}
    elif isinstance(have, list) and isinstance(value, list):
        new = have + [v for v in value if v not in have]
    elif have is _NONE or (have in ({}, []) and value not in ({}, [])):
        new = value
    else:
        return added
    samples[file] = new
    return added or have is _NONE or kind(new) != kind(have) or len(new) > len(have)


def skeleton(value):
    """``value`` with what differs call to call blanked: a clock reading, a fresh random id, and
    a count past 1 (each call can add one, so counts alone would never end discovery). Two
    values alike but for these are one alternative. A branch opened only by one particular
    count past 1, or one id, is not told apart: the bound discovery needs to end."""
    if isinstance(value, dict):
        return {skeleton(k): skeleton(v) for k, v in value.items()}
    if isinstance(value, list):
        return [skeleton(v) for v in value]
    if isinstance(value, float) or (type(value) is int and abs(value) > 10 ** 8):
        return "<clock>"
    if type(value) is int and value not in (0, 1):
        return "<n>"
    if isinstance(value, str) and len(value) >= 8 and set(value.lower()) <= set("0123456789abcdef-") \
            and len(set(value)) > 1:
        return "<id>"
    return value


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
        for value in alts.get(file, {}).values():
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
