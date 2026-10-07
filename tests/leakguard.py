#!/usr/bin/env python3
"""Leaked resources fail the run instead of scrolling past as warnings.

A ResourceWarning (an unclosed database, file, socket or a child process still running) is
almost always raised from a finalizer, where even ``-W error::ResourceWarning`` only turns it
into an "Exception ignored in ..." line and the run still passes. This module makes the
warning an error *and* catches it in ``sys.unraisablehook``, so each leak is charged to the test
(or harness group) that was running when the collector found it. Python children get the same
treatment from ``tests/leaksite/sitecustomize.py``: the guard puts that directory first on
``PYTHONPATH`` and names a log in ``DIAKTOROS_LEAK_LOG``; each child appends every leak there
as it happens (shutdown included) plus any child of its own left running in its session, and
the guard charges them to the running test, naming the child's argv:

    python tests/leakguard.py discover -s tests -p 'test_*.py'   # unittest, any argv it takes
    python tests/run_tests.py                                   # the harness installs it itself

Each leak names the statement that released it; to also see where it was allocated, run with
``PYTHONTRACEMALLOC=10`` (children inherit it). A child whose environment is scrubbed gets the
recorder from ``diaktoros.util.leak_guard_env``/``leak_guard_code``, which change nothing
unless this guard is running: supervisor fixture workers and fixture commands (via
``PYTHONPATH``), the seat-model resolver (``python -E -s -c``) and Git's askpass (loaded by
absolute path). A process inside the bubblewrap sandbox is not covered: the log is outside the
sandbox's fixed mount allowlist and its environment is set only by ``--setenv`` in
``contained.command``, the production sandbox argv, which this guard does not change.
"""

from __future__ import annotations

import atexit
import gc
import json
import os
from pathlib import Path
import subprocess
import tempfile
import traceback
import sys
import tracemalloc
import unittest
import warnings

_leaks: list[str] = []
_installed = False
SITE = Path(__file__).resolve().parent / "leaksite"
_child_log: Path | None = None
_hook = None
_child_offset = 0


def _describe(unraisable) -> str:
    text = _traced(f"{unraisable.exc_type.__name__}: {unraisable.exc_value}", unraisable.object)
    if "allocated at" not in text and unraisable.exc_traceback is not None:
        text += "\n  released at:\n" + "".join(
            "    " + line for line in traceback.format_tb(unraisable.exc_traceback))
    return text


def _traced(text: str, obj) -> str:
    where = tracemalloc.get_object_traceback(obj) if obj is not None else None
    if where is not None:
        text += "\n  allocated at:\n" + "\n".join("    " + line for line in where.format())
    return text


def install() -> None:
    """ResourceWarning is an error; a leak reported from a finalizer is recorded, not ignored."""
    global _installed
    if _installed:
        return
    _installed = True
    warnings.simplefilter("error", ResourceWarning)
    previous = sys.unraisablehook

    def hook(unraisable) -> None:
        if isinstance(unraisable.exc_value, ResourceWarning) or (
                unraisable.exc_type is not None and issubclass(unraisable.exc_type, ResourceWarning)):
            _leaks.append(_describe(unraisable))
        previous(unraisable)

    sys.unraisablehook = hook
    global _child_log, _hook
    _hook = hook
    fd, name = tempfile.mkstemp(prefix="leaks-", suffix=".jsonl")
    os.close(fd)
    _child_log = Path(name)
    atexit.register(_child_log.unlink, missing_ok=True)
    os.environ["DIAKTOROS_LEAK_LOG"] = name
    path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(SITE) + (os.pathsep + path if path else "")


def disarmed() -> list[str]:
    """Why this process's guard would not catch a leak; empty when it is fully armed.

    A lane that runs green without its guard is indistinguishable from a clean one, so a lane
    checks this before and after its run instead of trusting that install() was reached.
    """
    problems = []
    if not _installed or _child_log is None:
        problems.append("leakguard.install() never ran in this process")
    elif os.environ.get("DIAKTOROS_LEAK_LOG") != str(_child_log):
        problems.append("DIAKTOROS_LEAK_LOG does not name this run's child log")
    if _hook is None or sys.unraisablehook is not _hook:
        problems.append("sys.unraisablehook is not the guard's")
    first = next((f for f in warnings.filters if issubclass(ResourceWarning, f[2])), None)
    if first is None or first[0] != "error":
        problems.append(f"ResourceWarning is not an error (first matching filter: {first})")
    return problems


def _child_leaks() -> list[str]:
    """Leaks Python children appended to the log since the last read."""
    global _child_offset
    if _child_log is None:
        return []
    with open(_child_log, "rb") as log:
        log.seek(_child_offset)
        data = log.read()
    complete = data[:data.rfind(b"\n") + 1]   # a line still being written waits for next time
    _child_offset += len(complete)
    found = []
    for line in complete.splitlines():
        record = json.loads(line)
        for leak in record["leaks"]:
            found.append(f"in child {record['pid']} {record['argv']!r}:\n{leak}")
    return found


def rearm() -> list[str]:
    """What had disarmed the guard (see ``disarmed``), after re-arming it so the rest of the run
    is guarded again. Nothing can be re-armed before ``install()`` has run."""
    problems = disarmed()
    if problems and _installed and _child_log is not None:
        warnings.simplefilter("error", ResourceWarning)
        sys.unraisablehook = _hook
        os.environ["DIAKTOROS_LEAK_LOG"] = str(_child_log)
    return problems


def drain() -> list[str]:
    """Collect garbage now and return (and forget) every leak reported since the last drain."""
    gc.collect()
    found = _leaks[:]
    del _leaks[:]
    return found + _child_leaks()


def running_children() -> list[str]:
    """Child processes this run started and never waited for: each one outlives the suite, and
    its Popen warns "still running" only at interpreter exit, after the verdict is in."""
    return [_traced(f"subprocess {obj.pid} is still running: {obj.args!r}", obj)
            for obj in gc.get_objects()
            if isinstance(obj, subprocess.Popen) and obj.poll() is None]


class _LeakResult(unittest.TextTestResult):
    def stopTest(self, test) -> None:
        super().stopTest(test)
        for leak in drain():
            self.errors.append((test, f"resource leaked while this test ran:\n{leak}\n"))
        # Checked per test, inside the run: the runner restores warning filters when the run
        # ends, so a reset made by a test is invisible to any check after it.
        for problem in rearm():
            self.errors.append((test, f"leak guard was disarmed while this test ran: {problem}\n"))


class _LeakRunner(unittest.TextTestRunner):
    resultclass = _LeakResult

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # unittest.main hands the runner warnings='default', which the runner applies to every
        # category for the whole run: ResourceWarning would go back to a printed line.
        self.warnings = None


def main(argv: list[str]) -> int:
    install()
    if disarmed():                             # green without the guard would mean nothing
        for problem in disarmed():
            print(f"leak guard is not armed: {problem}", file=sys.stderr)
        return 3
    # Said up front, so a lane's log shows it ran guarded (CI's verticals job relies on this).
    print(f"leak guard armed: ResourceWarning is an error; child leaks go to {_child_log}",
          file=sys.stderr)
    # A class, not an instance, so unittest still applies -v, -f, -b and the rest to it.
    program = unittest.main(module=None, argv=["leakguard", *argv], testRunner=_LeakRunner,
                            exit=False)
    late = drain() + running_children()
    # After the last test: a module or class teardown can still replace the hook or the log.
    late += [f"leak guard is not armed after the run: {problem}" for problem in disarmed()]
    for leak in late:
        print(f"resource leaked after the last test:\n{leak}", file=sys.stderr)
    return 0 if program.result.wasSuccessful() and not late else 1


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[0] = os.getcwd()   # as ``python -m unittest`` would have it, not this script's dir
    sys.path.insert(1, here)
    import leakguard            # one guard module however it was started, not __main__ and a copy
    sys.exit(leakguard.main(sys.argv[1:]))
