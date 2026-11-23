"""What ``tests/_state_walk.py`` holds a call on junk to (#2303): the declared return type, the
result on a missing file, and, for a file the call rewrote, the method's own write on a missing
file (``healing``)."""
from __future__ import annotations

import inspect
import math
import re
import types
import typing

from _state_walk_cases import FRESH, HINTS


def conforms(value, hint) -> bool:
    """Whether ``value`` is of the return annotation ``hint`` (unions, generics, finite floats)."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if hint is None or hint is type(None):
        return value is None
    if origin in (typing.Union, types.UnionType):
        return any(conforms(value, arg) for arg in args)
    if hint is float:
        return type(value) in (int, float) and math.isfinite(value)
    if hint in (int, str):
        return type(value) is hint
    if origin is tuple:
        return (isinstance(value, tuple) and len(value) == len(args)
                and all(conforms(v, a) for v, a in zip(value, args)))
    if origin is list:
        return isinstance(value, list) and all(conforms(v, args[0]) for v in value)
    return isinstance(value, origin or hint)


def returns(cls, name: str):
    """The method's resolved return annotation (or its ``HINTS`` entry), else
    ``inspect.Signature.empty``."""
    if name in HINTS:
        return HINTS[name]
    try:
        return typing.get_type_hints(inspect.unwrap(getattr(cls, name))).get(
            "return", inspect.Signature.empty)
    except Exception:  # noqa: BLE001 - unresolvable: reported as no declared return type
        return inspect.Signature.empty


_STAMP = re.compile(r"\d{4}-\d\d-\d\d[T ][\d:.]+(?:Z|[+-]\d\d:?\d\d)?")


def plain(value):
    """``value`` with what differs between two calls a moment apart masked: a timestamp (a
    number past 1e8, or a fractional one, or an ISO time in a string). Counts and other small
    whole numbers stay, so a count of junk entries is not read as the count on a missing file."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return "#" if abs(value) >= 1e8 or value != int(value) else value
    if isinstance(value, str):
        return _STAMP.sub("#", value)
    if isinstance(value, dict):
        return {str(k): "#" if k in FRESH else plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    return repr(value)


def strings(value) -> set[str]:
    """Every string value (not key) anywhere in ``value``."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return set().union(*map(strings, value)) if value else set()
    return set()


def kind(value) -> str:
    if isinstance(value, bool) or value is None:
        return type(value).__name__
    return "number" if isinstance(value, (int, float)) else type(value).__name__


def healing(now, clean, junk: set[str], where: str = "") -> list[str]:
    """Where ``now`` (a file the call rewrote over junk) does not carry what ``clean`` (the
    method's own write on a missing file) holds: every path ``clean`` writes is there, of the
    same JSON type, with no planted ``junk`` string left at it (a leaf, or an item of a list it
    writes, which also keeps to the types of the items it writes). Keys ``clean`` does not write
    (another PR's entry, another method's field) are not this call's to heal."""
    at = where or "the top level"
    if kind(now) != kind(clean):
        return [f"holds a {kind(now)} at {at}, where a clean call writes a {kind(clean)}"]
    if isinstance(clean, dict):
        problems = []
        for key, value in clean.items():
            if key not in now:
                problems.append(f"lacks {where}/{key}, which a clean call writes")
            else:
                problems += healing(now[key], value, junk, f"{where}/{key}")
        return problems
    if isinstance(clean, list):
        kinds = {kind(v) for v in clean}
        bad = [v for v in now if (kinds and kind(v) not in kinds) or strings(v) & junk]
        return [f"kept {bad!r:.60} in the list at {at}"] if bad else []
    return [f"kept junk {now!r} at {at}"] if now in junk else []
