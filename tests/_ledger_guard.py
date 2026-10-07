"""Ledger guard for the tests: no test may open the operator's real run ledger (#108).

It wraps ``Supervisor.__init__`` so that a ledger (or a presence marker) under the real user's
``~/.hermes`` — found from the password database, not from ``HOME`` or ``HERMES_HOME``, which
tests repoint — is refused before anything is read or written. The wrapper is process-wide,
so one import during an unfiltered ``discover`` (``test_run_supervisor`` and the modules
that use ``module_home()`` import it) covers every test in the run; the harness
(``run_tests.py``) imports it too. A filtered run of another module alone does not install it;
#101's whole-suite guard covers that case once merged.

This is narrower than, and composes with, the whole-suite ``tests/_home_guard.py`` of #101
(temp HOME/HERMES_HOME, hermes shim, ``RealHomeError`` tripwires; imported first by every test).
Intended final state once both are merged: ``_home_guard`` stays each test's first import, and
imports ``_ledger_guard`` at its end, so every test and the harness get both.
"""
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_loop import config, run_supervisor  # noqa: E402

REAL_HERMES = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes"
# With #101's tripwire in the tree, a refusal here is also a ``config.RealHomeError``, so either
# guard's tests accept the other's refusal.
_TRIPWIRE = getattr(config, "RealHomeError", None)


class RealHomeTouched(_TRIPWIRE or RuntimeError):
    """A test reached the operator's real Hermes home."""


# What a refusal of the real ledger can raise: this guard's, or #101's tripwire when present —
# never a bare RuntimeError, which an unrelated failure could satisfy.
REFUSED = (RealHomeTouched,) + ((_TRIPWIRE,) if _TRIPWIRE else ())


def under_real_home(path) -> bool:
    """Is ``path`` the real ``~/.hermes`` or inside it, as spelled or through any symlink?"""
    path = os.path.expanduser(str(path))
    reals = {os.path.abspath(REAL_HERMES), os.path.realpath(REAL_HERMES)}
    forms = {os.path.abspath(path), os.path.realpath(path)}
    return any(form == real or form.startswith(real + os.sep) for form in forms for real in reals)


def _install() -> None:
    original = run_supervisor.Supervisor.__init__
    if getattr(original, "_ledger_guarded", False):
        return

    def guarded(self, db, *args, **kwargs):
        presence = kwargs.get("presence")
        if kwargs.get("create", True) and presence is None and \
                run_supervisor._canonical(db) == run_supervisor._canonical(
                    run_supervisor.production_ledger()):
            presence = run_supervisor.presence_marker()  # the default the host would use
        for path in (db, presence):
            if path is not None and under_real_home(path):
                raise RealHomeTouched(f"a test opened {path}, under the real {REAL_HERMES}; "
                                      "pin HERMES_HOME (and DIAKTOROS_CONFIG_DIR) to a temp dir")
        return original(self, db, *args, **kwargs)

    guarded._ledger_guarded = True
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
            "DIAKTOROS_CONFIG_DIR": os.path.join(temp.name, "review-loops.d")})
        env.start()
        held[:] = [env, temp]

    def teardown():
        env, temp = held
        env.stop()
        temp.cleanup()

    return setup, teardown
