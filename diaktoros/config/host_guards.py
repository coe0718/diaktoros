"""The test guards (#676): tripwires that stop the suite reaching the operator's real home,
real network, or real Hermes. Armed only by tests/_home_guard.py; a real loop never arms them.
"""
from __future__ import annotations

import ipaddress
import os
import pathlib
import re
from urllib.parse import urlsplit

from .. import envnames


# Set by the test suites' home guard (tests/_home_guard.py). While it is set, the roots the loop
# writes state under are checked against the operator's real Hermes home — ``home()`` (and so
# everything derived from it), ``state_dir(loop)`` (every write rooted in a loop's state_dir goes
# through it), the ``DIAKTOROS_CONFIG_DIR``/``DIAKTOROS_SUBS`` overrides, the run ledger and
# the supervisor's host home — so a test that escapes the guard fails loudly instead of writing
# to a real ledger or runtime file.
TEST_HOME_GUARD_ENV = envnames.name("TEST_HOME_GUARD")


# ...and the path of a sentinel file the guard creates in its own temp home. The tripwires arm only
# with both: the variable alone — say inherited by a real loop's gateway — arms nothing. Only
# tests/_home_guard.py creates the sentinel (an empty file, no secret in it).
TEST_GUARD_SENTINEL_ENV = envnames.name("TEST_GUARD_SENTINEL")


# Every tripwire message starts with this, so an operator who meets one knows what to clear.
_ARMED = (f"test guard active ({TEST_HOME_GUARD_ENV}=1): unset {TEST_HOME_GUARD_ENV} if this is a "
          "real loop (docs/operations.md#the-loop-stops-with-realhomeerror-or-realnetworkerror)")


# Test-only: one more directory to treat as "the real home" while the guard is on, so the
# tripwire itself can be proven against a fake home. It adds protection, never removes it.
TEST_REAL_HOME_ENV = envnames.name("TEST_REAL_HOME")


class RealHomeError(BaseException):
    """A guarded test resolved a path inside the operator's real Hermes home.

    A ``BaseException``, like ``KeyboardInterrupt``: the loop fails closed with broad ``except
    Exception`` handlers, and a tripwire those handlers swallowed would let the test pass.
    """


def test_guard_active() -> bool:
    """Are the test tripwires armed? Only under the test harness: ``DIAKTOROS_TEST_HOME_GUARD=1``
    *and* ``DIAKTOROS_TEST_GUARD_SENTINEL`` naming the sentinel file tests/_home_guard.py made.

    Either spelling of each arms it (the ``REVIEW_LOOP_`` names from before the rename): a guard
    that stopped hearing an old name would quietly let a test reach the real home."""
    if "1" not in (os.environ.get(n) for n in envnames.both("TEST_HOME_GUARD")):
        return False
    sentinels = [os.environ.get(n) for n in envnames.both("TEST_GUARD_SENTINEL")]
    try:
        return any(sentinel and pathlib.Path(sentinel).is_file() for sentinel in sentinels)
    except OSError:
        return False


def _real_homes() -> list[pathlib.Path]:
    homes = []
    try:
        import pwd
        homes.append(pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):
        pass
    for extra in envnames.both("TEST_REAL_HOME"):
        if os.environ.get(extra):
            homes.append(pathlib.Path(os.environ[extra]))
    return [pathlib.Path(os.path.normpath(h.absolute())) for h in homes] + [h.resolve() for h in homes]


def guard_real_home(path: pathlib.Path | str) -> pathlib.Path:
    """Return ``path``; under the test guard, raise if it is the real home or anywhere inside it.

    The whole home, not only its ``.hermes``: the same scope as ``guard_real_hermes`` and as the
    test guard's own ``_under_a_home``. A guarded process whose ``HOME`` was inherited as
    ``<real home>/projects/x`` would otherwise write ``<real home>/projects/x/.hermes`` freely.
    """
    path = pathlib.Path(path)
    if not test_guard_active():
        return path
    homes = _real_homes()
    # The lexical form first, so the obvious escape is refused without touching the real home;
    # then the resolved form, so a symlink into it is refused too.
    lexical = pathlib.Path(os.path.normpath(path.expanduser().absolute()))
    for candidate in (lexical, None):
        candidate = candidate or path.expanduser().resolve()
        for real in homes:
            if candidate == real or real in candidate.parents:
                raise RealHomeError(f"{_ARMED}. Otherwise a test escaped tests/_home_guard.py: "
                                    f"{path} resolves into the real home {real}")
    return path


class RealNetworkError(BaseException):
    """A guarded test tried to reach a real host (GitHub, Discord, a model upstream).

    A ``BaseException`` for the same reason as ``RealHomeError``: the loop's broad ``except
    Exception`` would otherwise turn the escape into an ordinary "network failed" and a pass.
    """


# Git's scp-like remote syntax: [user@]host:path, with the colon before any slash.
_SCP_REMOTE = re.compile(r"^(?:[^@/:]+@)?(\[[^\]/]+\]|[^/:]+):")


def guard_network(url: str) -> str:
    """Return ``url``; under the test guard, raise unless it stays on this machine.

    Loopback hosts (127.0.0.0/8, ::1, localhost) and local paths/``file:`` URLs are the tests'
    own fakes and pass. Everything else — above all api.github.com and github.com — means a test
    mocked one seam (say ``gh.api``) and not the one underneath (``gh.fetch``). Git's scp-style
    ``[user@]host:path`` (a colon before any slash) names a host, not a local path.

    Fails closed: only a schemed URL (``scheme://…``, ``file:`` included, ``tcp://host:port`` for
    a raw connect), an scp-style remote, or an absolute or ``./``/``../`` local path is judged at
    all. Anything else — a bare ``github.com``, ``gateway``, an empty string — is refused.
    """
    if not test_guard_active():
        return url
    if url.startswith(("/", "./", "../")):
        return url                                        # a local path
    scp = None if "://" in url else _SCP_REMOTE.match(url)
    parts = urlsplit(url)
    if scp:
        host = scp.group(1).strip("[]")
    elif "://" in url or parts.scheme == "file":
        host = parts.hostname or ""
        if not host and parts.scheme == "file":
            return url                                    # file:///path, file:/path
    else:
        host = ""                                         # a bare name: nothing to judge
    if host == "localhost":
        return url
    try:
        if ipaddress.ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise RealNetworkError(f"{_ARMED}. Otherwise: real network call to {url} refused; mock the request "
                           "underneath (gh.fetch, not only gh.api), set DIAKTOROS_GH_STUB, or "
                           "point it at a 127.0.0.1 fake")


def guard_real_hermes(executable: str) -> str:
    """Return ``executable``; under the test guard, raise if it is a ``hermes`` in the real home.

    The guard also shadows ``hermes`` on PATH with a shim that refuses to run; this is the second
    layer, for a PATH the shim is missing from.

    The same scope as ``guard_real_home``, the whole real home: a ``hermes`` anywhere under it —
    ``~/.local/bin/hermes`` included — is the operator's own. The test guard therefore keeps its
    temp root outside every protected home, and never creates or writes its shim under one: an
    inherited shim dir there is replaced by a temp dir outside.
    """
    if not test_guard_active() or not os.path.isabs(executable):
        return executable
    found = pathlib.Path(executable)
    resolved = [pathlib.Path(os.path.normpath(found.absolute())), found.resolve()]
    for real in _real_homes():
        if any(real in path.parents for path in resolved):
            raise RealHomeError(f"{_ARMED}. Otherwise a test escaped tests/_home_guard.py: "
                                f"refusing to run the real {executable}")
    return executable
