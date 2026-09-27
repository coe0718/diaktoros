#!/usr/bin/env python3
"""Leaked resources fail the run instead of scrolling past as warnings.

A ResourceWarning (an unclosed database, file, socket or a child process still running) is
almost always raised from a finalizer, where even ``-W error::ResourceWarning`` only turns it
into an "Exception ignored in ..." line and the run still passes. This module makes the
warning an error *and* catches it in ``sys.unraisablehook``, so each leak is charged to the test
(or harness group) that was running when the collector found it:

    python tests/leakguard.py discover -s tests -p 'test_*.py'   # unittest, any argv it takes
    python tests/run_tests.py                                   # the harness installs it itself

To see where a leaked object was allocated, run with ``PYTHONTRACEMALLOC=10``.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import tracemalloc
import unittest
import warnings

_leaks: list[str] = []
_installed = False


def _describe(unraisable) -> str:
    text = f"{unraisable.exc_type.__name__}: {unraisable.exc_value}"
    where = tracemalloc.get_object_traceback(unraisable.object) if unraisable.object else None
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


def drain() -> list[str]:
    """Collect garbage now and return (and forget) every leak reported since the last drain."""
    gc.collect()
    found = _leaks[:]
    del _leaks[:]
    return found


def running_children() -> list[str]:
    """Child processes this run started and never waited for: each one outlives the suite, and
    its Popen warns "still running" only at interpreter exit, after the verdict is in."""
    return [f"subprocess {obj.pid} is still running: {obj.args!r}" for obj in gc.get_objects()
            if isinstance(obj, subprocess.Popen) and obj.poll() is None]


class _LeakResult(unittest.TextTestResult):
    def stopTest(self, test) -> None:
        super().stopTest(test)
        for leak in drain():
            self.errors.append((test, f"resource leaked while this test ran:\n{leak}\n"))


def main(argv: list[str]) -> int:
    install()
    runner = unittest.TextTestRunner(resultclass=_LeakResult)
    program = unittest.main(module=None, argv=["leakguard", *argv], testRunner=runner, exit=False)
    late = drain() + running_children()
    for leak in late:
        print(f"resource leaked after the last test:\n{leak}", file=sys.stderr)
    return 0 if program.result.wasSuccessful() and not late else 1


if __name__ == "__main__":
    import os
    sys.path[0] = os.getcwd()   # as ``python -m unittest`` would have it, not this script's dir
    sys.exit(main(sys.argv[1:]))
