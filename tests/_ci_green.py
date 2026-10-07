"""Green CI for tests whose GitHub fake predates the CI gate (``diaktoros.ci``).

The broker refuses an APPROVE while CI cannot be read, so a ``gh.api`` fake that knows nothing of
check runs makes every approval fail closed. ``green(fake)`` answers the two CI reads with "no
checks reported" and hands every other call to ``fake`` unchanged. ``tests/test_ci_gate.py``
covers the CI reads and refusals themselves.
"""
import re

_STATUS = re.compile(r"/commits/[^/]+/status$")


def green(fake):
    def api(loop, path, *args, **kwargs):
        method = kwargs.get("method", args[0] if args else "GET")
        if method == "GET" and "/check-runs" in path:
            return {"total_count": 0, "check_runs": []}
        if method == "GET" and _STATUS.search(path):
            return {"state": "pending", "statuses": []}
        return fake(loop, path, *args, **kwargs)
    return api
