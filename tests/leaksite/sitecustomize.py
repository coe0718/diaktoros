"""The child half of tests/leakguard.py: a Python child's leaks fail the suite too.

The guard puts this directory first on ``PYTHONPATH`` and names a log in
``DIAKTOROS_LEAK_LOG``, so every Python child the suite starts (gate scripts, the watchdog,
cleanup, supervisor workers) imports this at startup. ResourceWarning becomes an error, a leak
reported from a finalizer is recorded with where it was released (and, under
``PYTHONTRACEMALLOC``, where it was allocated), a child process still running at exit counts as
a leak too (unless it was detached into its own session), and at exit the
child appends one JSON line (its pid, argv and leaks) to the log for the guard to charge to the
test that was running. Outside the guard neither variable is set and this file is never on the
path.
"""

import os

if os.environ.get("DIAKTOROS_LEAK_LOG"):
    import atexit
    import gc
    import json
    import re
    import sys
    import traceback
    import tracemalloc
    import warnings

    _log = os.environ["DIAKTOROS_LEAK_LOG"]   # as the child started, whatever it sets later
    # Tracing every child costs the harness several minutes; a leak released by a statement
    # carries that statement's traceback anyway. PYTHONTRACEMALLOC=N adds allocation sites.
    # A distribution's crash reporter (Fedora's ABRT) hooks uncaught exceptions and leaks its
    # own socket; a fixture child that dies on purpose must not file a system crash report.
    if "abrt" in (getattr(sys.excepthook, "__module__", None) or ""):
        sys.excepthook = sys.__excepthook__
    warnings.simplefilter("error", ResourceWarning)
    _previous = sys.unraisablehook
    _reported_pids = set()
    _STILL_RUNNING = re.compile(r"subprocess (\d+) is still running")

    def _record(leak, _dumps=json.dumps, _open=os.open, _write=os.write, _close=os.close,
                _flags=os.O_WRONLY | os.O_APPEND | os.O_CREAT, _pid=os.getpid(), _argv=sys.argv):
        """Append one leak now: a leak found while the interpreter shuts down (a module global
        released after atexit) must reach the log too, so nothing here may need a global."""
        fd = _open(_log, _flags)
        try:
            _write(fd, (_dumps({"pid": _pid, "argv": _argv, "leaks": [leak]}) + "\n").encode())
        finally:
            _close(fd)

    def _traced(text, obj):
        where = tracemalloc.get_object_traceback(obj) if obj is not None else None
        if where is not None:
            text += "\n  allocated at:\n" + "\n".join("    " + line for line in where.format())
        return text

    def _detached(pid):
        """A child that leads its own session (start_new_session=True) was handed off on
        purpose, as a supervisor worker hands the queue to its successor; one left running in
        this process's session is a leak."""
        try:
            return os.getsid(pid) == pid
        except OSError:
            return False

    def _hook(unraisable):
        if isinstance(unraisable.exc_value, ResourceWarning) or (
                unraisable.exc_type is not None and issubclass(unraisable.exc_type, ResourceWarning)):
            running = _STILL_RUNNING.search(str(unraisable.exc_value))
            if running and (int(running.group(1)) in _reported_pids
                            or _detached(int(running.group(1)))):
                return   # already reported at exit, or handed off on purpose
            text = _traced(f"{unraisable.exc_type.__name__}: {unraisable.exc_value}",
                           unraisable.object)
            if "allocated at" not in text and unraisable.exc_traceback is not None:
                text += "\n  released at:\n" + "".join(
                    "    " + line for line in traceback.format_tb(unraisable.exc_traceback))
            _record(text)
        _previous(unraisable)

    sys.unraisablehook = _hook

    def _report():
        gc.collect()
        popen = sys.modules.get("subprocess")
        if popen is None:
            return
        for obj in gc.get_objects():
            if isinstance(obj, popen.Popen) and obj.poll() is None and not _detached(obj.pid):
                _reported_pids.add(obj.pid)
                _record(_traced(f"subprocess {obj.pid} is still running: {obj.args!r}", obj))

    atexit.register(_report)
