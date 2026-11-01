"""The watchdog's shared basics: its test mode, and how it reads the clocks it keeps.

The watchdog script and the modules split out of it read these from here, one binding each,
so patching ``watch_mode.TEST`` in a test reaches every reader.
"""

from __future__ import annotations

import math

from diaktoros import envnames

# Zero-grace test mode (``DIAKTOROS_TEST=1``): no paused check, no grace, no cooldown. Every
# watchdog piece reads it here as ``watch_mode.TEST`` (never a copy), so a test that patches it
# reaches all of them.
TEST = bool(envnames.get("TEST"))


def valid_clock(value: object, now: float) -> float | None:
    """Treat corrupt or future persisted clocks as unknown, never as a grace deadline."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clock = float(value)
    return clock if math.isfinite(clock) and 0 < clock <= now else None


def alert_due(watch: dict, key: str, now: float, cooldown: float) -> bool:
    """True at most once per ``cooldown`` for ``key``, and again once each cooldown has elapsed.

    The edge is inclusive: the first call at exactly ``cooldown`` after the last stamp fires (``now
    - last < cooldown`` is False there), and the call just before it is silent. Stamped only when it
    fires: a condition that persists re-alerts every cooldown instead of refreshing its own clock on
    every sweep and never speaking again (#77, as the stall map does).
    """
    marks = watch.get("github_alerts")
    if not isinstance(marks, dict):
        marks = watch["github_alerts"] = {}
    last = valid_clock(marks.get(key), now)
    if last is not None and now - last < cooldown:
        return False
    marks[key] = now
    return True
