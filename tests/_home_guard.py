"""Test-suite guard: no test may open the operator's real run ledger (#108).

Every test module imports this. It wraps ``Supervisor.__init__`` so that a ledger (or a
presence marker) under the real user's ``~/.hermes`` — found from the password database, not
from ``HOME`` or ``HERMES_HOME``, which tests repoint — is refused before anything is read or
written. A test that reaches the production ledger must pin ``HERMES_HOME`` to a temp dir.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_loop import run_supervisor  # noqa: E402

REAL_HERMES = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes"


class RealHomeTouched(RuntimeError):
    """A test reached the operator's real Hermes home."""


def under_real_home(path) -> bool:
    real = os.path.abspath(REAL_HERMES)
    candidate = os.path.abspath(os.path.expanduser(str(path)))
    return candidate == real or candidate.startswith(real + os.sep)


def _install() -> None:
    original = run_supervisor.Supervisor.__init__
    if getattr(original, "_home_guarded", False):
        return

    def guarded(self, db, *args, **kwargs):
        presence = kwargs.get("presence")
        if kwargs.get("create", True) and presence is None and \
                os.path.abspath(db) == os.path.abspath(run_supervisor.production_ledger()):
            presence = run_supervisor.presence_marker()  # the default the host would use
        for path in (db, presence):
            if path is not None and under_real_home(path):
                raise RealHomeTouched(f"a test opened {path}, under the real {REAL_HERMES}; "
                                      "pin HERMES_HOME (and REVIEW_LOOP_CONFIG_DIR) to a temp dir")
        return original(self, db, *args, **kwargs)

    guarded._home_guarded = True
    run_supervisor.Supervisor.__init__ = guarded


_install()


def module_home():
    """``setUpModule, tearDownModule = _home_guard.module_home()``: pin a temp Hermes home.

    For a module whose tests reach code that reads ``config.home()`` (gate scripts, observer,
    explain), so they neither open the real ledger nor trip the guard.
    """
    import tempfile
    from unittest import mock
    held = []

    def setup():
        temp = tempfile.TemporaryDirectory(prefix="rl-home-")
        env = mock.patch.dict(os.environ, {
            "HERMES_HOME": temp.name,
            "REVIEW_LOOP_CONFIG_DIR": os.path.join(temp.name, "review-loops.d")})
        env.start()
        held[:] = [env, temp]

    def teardown():
        env, temp = held
        env.stop()
        temp.cleanup()

    return setup, teardown
