"""The number parsers ``normalize`` and its checks share (split from the core, which sits at its
1,500 size ceiling): a whole-number setting (``_as_int``), a string that spells a number
(``_numeric_text``, #1692) and a positive, finite, bounded number (``_positive_number``, #2411).
They reach the core through ``config`` (``from .. import config``), so ``ConfigError``,
``DEFAULTS`` and ``POSITIVE_NUMBER_MAX`` stay one binding; callers use ``number_settings.<name>``.
"""
from __future__ import annotations

import math

from .. import config


def _as_int(value, key: str, where: str) -> int:
    """An integer setting, or a config.ConfigError naming it (``int("many")`` must not escape)."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        # int(3.5) would quietly become 3: a hand-edited fraction is a mistake, not a setting.
        raise config.ConfigError(f"{where}: {key!r} must be a whole number, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):     # int(float("inf")): JSON allows Infinity
        raise config.ConfigError(f"{where}: {key!r} must be a whole number, got {value!r}") from None


def _numeric_text(value):
    """``value`` itself, or a string that spells a number (``"6"``, ``" 0.5 "``) as that number
    (#1692): an int when it is one, else a float. Text that is not a number comes back unchanged
    for the caller to refuse."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    for read in (int, float):
        try:
            return read(text)
        except (ValueError, OverflowError):   # also an int string past the 4300-digit limit
            continue
    return value


def _positive_number(value, key: str, where: str):
    """A positive, finite number setting (``null`` or ``""`` = the default), or a config.ConfigError
    naming it. The watchdog does ``float(...)``/``int(...)`` on these mid-sweep, so other text,
    a bool, zero, a negative or ``Infinity``/``NaN`` must fail when the loop file loads, and so
    must a value past ``POSITIVE_NUMBER_MAX`` (one year), which the readers' unit conversion
    would turn into an infinite clock (#2411). A string that spells a number (``"6"``) is stored
    as that number, the way ``_as_int`` reads one for the integer settings (#1692)."""
    if value is None or value == "":
        return config.DEFAULTS[key]
    value = _numeric_text(value)
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok:
        try:
            # float() is what the watchdog does; math.isfinite converts too, so a huge int
            # raises OverflowError. Refuse an out-of-range number here instead of leaking it.
            ok = math.isfinite(float(value)) and value > 0
        except OverflowError:
            ok = False
    top = config.POSITIVE_NUMBER_MAX[key]
    if ok and value > top:
        raise config.ConfigError(f"{where}: {key!r} must be a positive number up to {top}, got "
                          f"{value!r}")
    if not ok:
        try:
            text = repr(value)
        except (ValueError, RecursionError):   # an int past the 4300-digit str limit, even nested
            text = f"an unprintable {type(value).__name__}"
        text = text if len(text) <= 60 else text[:57] + "..."   # not a 400-digit integer whole
        raise config.ConfigError(f"{where}: {key!r} must be a positive number, got {text}")
    return value
