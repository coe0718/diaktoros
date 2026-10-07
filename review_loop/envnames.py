"""The environment variables this plugin reads (#425 stage 4).

Each is ``DIAKTOROS_<NAME>``; the name from before the rename, ``REVIEW_LOOP_<NAME>``, is still read
when the new one is not set, so an operator's gateway environment or a script that sets the old
name keeps working. The plugin itself only ever sets the new names. No imports from the package:
every module may use this, ``util`` included.
"""
from __future__ import annotations

import os

PREFIX, OLD_PREFIX = "DIAKTOROS_", "REVIEW_LOOP_"


def name(short: str) -> str:
    """The variable's name, as the plugin sets it."""
    return PREFIX + short


def both(short: str) -> tuple[str, str]:
    return PREFIX + short, OLD_PREFIX + short


def get(short: str, default=None):
    """The variable's value under its new name, else its old one, else ``default``."""
    for full in both(short):
        value = os.environ.get(full)
        if value is not None:
            return value
    return default
