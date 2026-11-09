"""Which process claims a run, and whether its code is the installed plugin's (#1337).

Runs were sometimes claimed by code older than the installed plugin: a claim-time read failed
with an error only a copy from before the reinstall could raise, and nothing recorded which
process made the claim. Two answers here:

* ``identity()``: the claimant's PID, executable, entry script and plugin commit, which every
  claim-time read failure records, so the next one names its process.
* ``stale()``: whether this process loaded the plugin at a commit other than the one installed
  now (a gateway that loads plugins once at start, a watchdog started before a reinstall). Such a
  claimant refuses to claim; the run stays pending for a worker running the installed code.

The commit is the plugin checkout's HEAD, read when this module is first imported (the code this
process loaded) and again at each check (the code installed now). A plugin that is not a git
checkout has no commit to compare, so it is never called stale.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys


def plugin_root() -> pathlib.Path:
    """The installed plugin's checkout: the directory holding the ``diaktoros`` package."""
    return pathlib.Path(__file__).resolve().parents[1]


def installed_commit() -> str:
    """The plugin checkout's HEAD now, or "" when it is not a git checkout or git is unreadable."""
    try:
        proc = subprocess.run(["git", "-C", str(plugin_root()), "rev-parse", "HEAD"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""


# The commit this process loaded: read once, at first import.
LOADED = installed_commit()


def stale(installed: str | None = None) -> str:
    """Why this process must not claim (its code is older than the install), or "".

    Only a known commit on both sides counts: an unknown one is never a reason to refuse."""
    now = installed_commit() if installed is None else installed
    if LOADED and now and LOADED != now:
        return (f"this process loaded plugin {LOADED[:7]}, but {now[:7]} is installed: "
                "it refuses to claim, and a worker on the installed code claims the run")
    return ""


def identity(installed: str | None = None) -> str:
    """The claimant, in one bounded line: PID, executable, entry script and plugin commit, with
    the installed commit too when it differs from the loaded one. The executable and entry are
    each cut to their last 80 characters, so the line stays under about 220."""
    script = pathlib.PurePath(sys.argv[0]).parts[-2:] if sys.argv and sys.argv[0] else ()
    entry = "/".join(script) or "unknown"
    plugin = LOADED[:7] or "unknown"
    now = installed_commit() if installed is None else installed
    if LOADED and now and now != LOADED:
        plugin += f", installed {now[:7]}"
    # Each unbounded field keeps its tail (#2218): the commit, last, is never cut off.
    return (f"claimant pid {os.getpid()} {_tail(sys.executable or 'unknown')} {_tail(entry)}, "
            f"plugin {plugin}")


def _tail(text: str, limit: int = 80) -> str:
    """``text`` cut to its last ``limit`` characters, marked with a leading ellipsis when cut."""
    return text if len(text) <= limit else "…" + text[-(limit - 1):]
