"""Small shared helpers. No third-party imports anywhere in this package."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import NoReturn
from . import envnames

# What every line the plugin logs starts with. ``trace`` and ``review`` read a gate's reasons back
# by it, so they also accept the prefix from before the rename (#425), in logs already written.
LOG_PREFIX = "[diaktoros]"
LOG_PREFIXES = (LOG_PREFIX, "[review-loop]")


def logged(lines) -> list[str]:
    """What each of ``lines`` the plugin logged says, without its prefix (either spelling)."""
    return [line.split("] ", 1)[1] for line in lines
            if line.startswith(tuple(f"{prefix} " for prefix in LOG_PREFIXES))]

# The test suite's leak recorder for child processes (tests/leakguard.py). Only that guard sets
# DIAKTOROS_LEAK_LOG, and only this checkout's own file is ever loaded: the variable is a
# switch and a log path, never a code path. With it unset, every helper below returns its
# input unchanged, so a production child's argv, script and environment are byte-identical.
_LEAK_SITE = Path(__file__).resolve().parents[1] / "tests" / "leaksite" / "sitecustomize.py"


def log(message: str, quiet: bool = False) -> None:
    """Diagnostics go to stderr always — a gate's stdout is a protocol, not a console."""
    if not quiet:
        print(f"{LOG_PREFIX} {message}", file=sys.stderr)


def leak_guard() -> str | None:
    """The leak log when tests/leakguard.py runs this process, else None."""
    log = envnames.get("LEAK_LOG")
    return log if log and _LEAK_SITE.is_file() else None


def leak_guard_env(env: dict, *, pythonpath: bool = True) -> dict:
    """Under the guard only: carry the recorder into a child whose environment is scrubbed.

    ``pythonpath`` puts the recorder's directory first on the child's ``PYTHONPATH``; a child
    started with ``-E`` ignores that, and loads it through ``leak_guard_code`` instead.
    """
    log = leak_guard()
    if log:
        env[envnames.name("LEAK_LOG")] = log
        if pythonpath:
            site = str(_LEAK_SITE.parent)
            env["PYTHONPATH"] = site + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def leak_guard_code(code: str) -> str:
    """Under the guard only: ``code`` (a ``-c`` program or a script body) loads the recorder
    first, by absolute path, so it works under ``-E -s`` where no path variable is read."""
    if not leak_guard():
        return code
    return ("import importlib.util as _lg\n"
            f"_lg_s = _lg.spec_from_file_location('sitecustomize', {str(_LEAK_SITE)!r})\n"
            "_lg_m = _lg.module_from_spec(_lg_s)\n"
            "__import__('sys').modules['sitecustomize'] = _lg_m\n"
            "_lg_s.loader.exec_module(_lg_m)\n"
            "del _lg, _lg_s, _lg_m\n" + code)


# Set by ``gate.context``: called with (reason, decision) so a declining gate leaves a record
# (#209). Best effort: whatever it does, the answer below is unchanged.
_DECISION_RECORDER = None


def silence(reason: str = "", decision: str = "declined") -> NoReturn:
    """The gate's "nothing to do" answer. The route adapter renders nothing for this.

    Typed ``NoReturn`` on purpose: every guard reads as "silence, *then* we know the event
    is ours", and the type checker needs to agree that the code after it is reachable only
    for events that passed.
    """
    if reason:
        log(reason)
        if _DECISION_RECORDER is not None:
            try:
                _DECISION_RECORDER(reason, decision)
            except BaseException:  # noqa: BLE001 - never change the gate's answer
                pass
    print("[SILENT]")
    raise SystemExit(0)


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} GB"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_at(when: float) -> str:
    """A local mark's epoch as the same UTC ISO-8601 GitHub stamps its own timestamps with.

    One spelling for every timestamp a report shows means an operator can compare them without
    wondering which clock wrote which line.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)) if when else ""


def epoch(iso: str | None) -> float:
    """GitHub timestamps are UTC ISO-8601. Parse as UTC, never as local time."""
    if not iso:
        return 0.0
    import calendar

    try:
        return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        try:
            from datetime import datetime

            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0


def age_min(iso: str | None) -> float:
    t = epoch(iso)
    return (time.time() - t) / 60 if t else 0.0
