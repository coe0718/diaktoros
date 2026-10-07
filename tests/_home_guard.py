"""Keep every test, and every process a test starts, out of the operator's real ``~/.hermes``.

Import this before anything from ``review_loop`` — every ``tests/test_*.py`` does so as its first
import (``test_home_guard.py`` enforces that), and ``run_tests.py`` does for the harness. On first
import in a process it:

* captures, for read-only use, the Rust toolchain (``RUSTUP_HOME``/``CARGO_HOME`` and
  ``USER_HOME`` below). The real-Hermes tests take their Hermes source only from an explicit
  ``HERMES_AGENT_SOURCE`` — a disposable checkout, never the live ``~/.hermes/hermes-agent``
  (``needs_real_hermes`` fails them loudly if it points there);
* points ``HOME`` and ``HERMES_HOME`` at a fresh temp directory and drops
  inherited overrides that could name real state, so ``config.home()``, ``Path.home()``, ``~``
  and every subprocess that inherits the environment (gate scripts, run_supervisor workers, the
  watchdog) land there. Both are created on import, in a guarded child too (never inside a
  real home);
* puts a ``hermes`` shim first on PATH that refuses to run (see ``FAKE_HERMES_ENV``);
* arms the plugin's tripwire (``DIAKTOROS_TEST_HOME_GUARD`` plus the sentinel file named by
  ``DIAKTOROS_TEST_GUARD_SENTINEL``, which only this module creates — the variable alone arms
  nothing): while armed, resolving the Hermes home, a ledger, a state dir or a cleanup root
  anywhere inside the real home raises ``config.RealHomeError`` — so a test that escapes this guard
  fails instead of writing.

unittest's ``discover -s tests`` never imports ``tests/__init__.py`` (the start directory is the
top level, not a package), which is why this is an explicit first import rather than a package hook.
"""

from __future__ import annotations

import atexit
import functools
import os
import pathlib
import shutil
import tempfile
import unittest

GUARD_ENV = "DIAKTOROS_TEST_HOME_GUARD"
# Inherited settings that could point a test at real state; the fixtures set their own.
_DROP = ("DIAKTOROS_CONFIG_DIR", "DIAKTOROS_SUBS", "DIAKTOROS_TOKEN_FILE")

_FRESH = not (os.environ.get(GUARD_ENV) == "1" and os.environ.get("DIAKTOROS_TEST_USER_HOME"))
USER_HOME = pathlib.Path.home() if _FRESH else pathlib.Path(os.environ["DIAKTOROS_TEST_USER_HOME"])


def _protected_homes() -> list[pathlib.Path]:
    homes = [USER_HOME]
    try:
        import pwd
        homes.append(pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):
        pass
    if os.environ.get("DIAKTOROS_TEST_REAL_HOME"):     # the plugin's test-only fake real home
        homes.append(pathlib.Path(os.environ["DIAKTOROS_TEST_REAL_HOME"]))
    return homes


def _under_a_home(path: pathlib.Path) -> bool:
    """Is ``path`` anywhere inside a protected home (not only its .hermes)?"""
    forms = {pathlib.Path(os.path.normpath(path.absolute())), path.resolve()}
    homes = {form for home in _protected_homes()
             for form in (pathlib.Path(os.path.normpath(home.absolute())), home.resolve())}
    return any(home == form or home in form.parents for home in homes for form in forms)


# The temp root must lie outside every protected home. config.guard_real_hermes refuses any
# `hermes` anywhere under the real home (a ~/.local/bin/hermes is as real as the install), so a
# TMPDIR under $HOME would put the temp home — and the shim in it — where the plugin refuses to
# run it. Rather than make the caller get TMPDIR right, the guard picks a root outside, and every
# process it starts inherits it.
if _under_a_home(pathlib.Path(tempfile.gettempdir())):
    for _root in ("/var/tmp", "/tmp"):
        if os.path.isdir(_root) and os.access(_root, os.W_OK | os.X_OK) \
                and not _under_a_home(pathlib.Path(_root)):
            os.environ["TMPDIR"] = _root
            tempfile.tempdir = None               # forget the cached, home-rooted choice
            break
    else:
        raise RuntimeError(f"tests/_home_guard.py: TMPDIR {tempfile.gettempdir()} is inside the "
                           "home and no temp root outside it is writable; set TMPDIR outside $HOME")

if not _FRESH:
    # Already guarded (a child of a guarded test): keep the parent's temp home.
    TEST_HOME = pathlib.Path(os.environ["HOME"])
else:
    for _var, _default in (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo")):
        if not os.environ.get(_var) and (USER_HOME / _default).is_dir():
            os.environ[_var] = str(USER_HOME / _default)
    TEST_HOME = pathlib.Path(tempfile.mkdtemp(prefix="review-loop-test-home-")).resolve()
    atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
    for _var in _DROP:
        os.environ.pop(_var, None)
    os.environ.update({"HOME": str(TEST_HOME), "HERMES_HOME": str(TEST_HOME / ".hermes"),
                       "DIAKTOROS_TEST_USER_HOME": str(USER_HOME), GUARD_ENV: "1"})

# The plugin's tripwires arm only with GUARD_ENV *and* this sentinel (config.test_guard_active), so
# the bare variable inherited by a real loop arms nothing. A guarded child keeps its parent's; one
# whose sentinel is missing (or never inherited) makes its own. Empty: nothing secret in it.
SENTINEL_ENV = "DIAKTOROS_TEST_GUARD_SENTINEL"
if not (os.environ.get(SENTINEL_ENV) and os.path.isfile(os.environ[SENTINEL_ENV])):
    _sentinel_dir = pathlib.Path(tempfile.mkdtemp(prefix="review-loop-test-guard-")).resolve()
    atexit.register(shutil.rmtree, _sentinel_dir, ignore_errors=True)
    (_sentinel_dir / "sentinel").touch()
    os.environ[SENTINEL_ENV] = str(_sentinel_dir / "sentinel")

# No guarded test, nor any process it starts, may run the operator's real `hermes` CLI: it acts on
# the real install (a bare `hermes` once resumed an interrupted source update and rebuilt the real
# hermes-agent's UI builds). A shim goes FIRST on PATH and fails loudly, unless a test names its
# own fake in FAKE_HERMES_ENV — and even then never the real binary.
FAKE_HERMES_ENV = "DIAKTOROS_TEST_FAKE_HERMES"
BLOCKED = "real hermes blocked under test guard"
SHIM_EXIT = 97
_SHIM = """#!/bin/sh
real={real}
refuse() {{ echo "{blocked}: $1" >&2; exit {code}; }}
inside() {{ case "$fake/" in "$1"/*) refuse "DIAKTOROS_TEST_FAKE_HERMES is inside a protected home ($1)";; esac; }}
if [ -n "$DIAKTOROS_TEST_FAKE_HERMES" ]; then
  fake=$(readlink -f -- "$DIAKTOROS_TEST_FAKE_HERMES")
  if [ -n "$real" ] && {{ [ "$fake" = "$(readlink -f -- "$real")" ] || [ "$fake" -ef "$real" ]; }}; then
    refuse "DIAKTOROS_TEST_FAKE_HERMES names the real binary"
  fi
  {home_checks}
  if [ -n "$DIAKTOROS_TEST_REAL_HOME" ]; then inside "$(readlink -f -- "$DIAKTOROS_TEST_REAL_HOME")"; fi
  exec "$DIAKTOROS_TEST_FAKE_HERMES" "$@"
fi
refuse "set DIAKTOROS_TEST_FAKE_HERMES to a fake (tests/_home_guard.py)"
"""


def shim_script(real: str, homes: list[pathlib.Path]) -> str:
    """The shim's text: ``real`` is the operator's hermes (refused as a fake, like any fake that
    resolves inside one of ``homes``)."""
    import shlex
    forms = sorted({str(form) for home in homes
                    for form in (pathlib.Path(os.path.normpath(home.absolute())), home.resolve())})
    checks = "\n  ".join(f"inside {shlex.quote(form)}" for form in forms) or ":"
    return _SHIM.format(real=shlex.quote(real), blocked=BLOCKED, code=SHIM_EXIT, home_checks=checks)


def _real_hermes(skip: set[pathlib.Path]) -> str:
    """The operator's ``hermes``, resolved from PATH with every shim dir (``skip``) and the test
    home stripped, and any guard shim ignored wherever it lives — never plain ``shutil.which``,
    whose first hit may be an inherited shim that would stand in for the real binary."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = pathlib.Path(entry)
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved in skip or TEST_HOME.resolve() in (resolved, *resolved.parents):
            continue
        candidate = directory / "hermes"
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            with candidate.open("rb") as stream:
                if BLOCKED.encode() in stream.read(4096):
                    continue                      # another guard's shim, not a hermes
        except OSError:
            continue
        return str(candidate)
    return ""
# Where the shim lives: the parent's (inherited), else inside a fresh process's own temp home,
# else — a guarded child with no inherited shim dir, whose HOME it did not make — a temp dir of
# its own. Never anywhere under a protected home, whatever was inherited: an inherited shim dir
# there is replaced, never created or written.
_INHERITED_SHIM = os.environ.get("DIAKTOROS_TEST_SHIM_DIR")
SHIM_DIR = pathlib.Path(_INHERITED_SHIM or TEST_HOME / ".review-loop-test-bin")
if (not _INHERITED_SHIM and not _FRESH) or _under_a_home(SHIM_DIR):
    SHIM_DIR = pathlib.Path(tempfile.mkdtemp(prefix="review-loop-test-bin-")).resolve()
    atexit.register(shutil.rmtree, SHIM_DIR, ignore_errors=True)
if not (SHIM_DIR / "hermes").exists():
    _skip = {SHIM_DIR.resolve()} | ({pathlib.Path(_INHERITED_SHIM).resolve()} if _INHERITED_SHIM else set())
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    (SHIM_DIR / "hermes").write_text(shim_script(_real_hermes(_skip), _protected_homes()))
    (SHIM_DIR / "hermes").chmod(0o755)
_path = os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join([str(SHIM_DIR), *(p for p in _path if p != str(SHIM_DIR))])
os.environ["DIAKTOROS_TEST_SHIM_DIR"] = str(SHIM_DIR)

# The Hermes source the opt-in real-Hermes tests run (in bwrap, by its venv's own `hermes`). Only
# ever an explicit HERMES_AGENT_SOURCE — there is no default, because the obvious default is the
# operator's live install, whose `hermes` acts on the real ~/.hermes.
HERMES_AGENT_SOURCE = (pathlib.Path(os.environ["HERMES_AGENT_SOURCE"])
                       if os.environ.get("HERMES_AGENT_SOURCE") else None)


def source_refusal(source: pathlib.Path | None = None) -> str:
    """Why the real-Hermes tests must not run against ``source``, or ``""``.

    Refused: a source at or inside a protected home's ``.hermes`` (the live install), lexically
    or once symlinks are resolved.
    """
    source = HERMES_AGENT_SOURCE if source is None else source
    if source is None:
        return ""
    forms = {pathlib.Path(os.path.normpath(source.absolute())), source.resolve()}
    for home in _protected_homes():
        for live in {pathlib.Path(os.path.normpath(home.absolute())) / ".hermes",
                     home.resolve() / ".hermes"}:
            if any(form == live or live in form.parents for form in forms):
                return (f"HERMES_AGENT_SOURCE={source} is inside the live Hermes install "
                        f"({live}); the real-Hermes tests refuse to run it. Point "
                        "HERMES_AGENT_SOURCE at a disposable hermes-agent checkout with its own "
                        "venv, outside ~/.hermes.")
    return ""


def needs_real_hermes(*prerequisites: bool, reason: str = "real-Hermes test prerequisites absent"):
    """Decorate an opt-in real-Hermes test (function or class).

    A HERMES_AGENT_SOURCE inside the live install fails the test loudly, whatever else is
    missing. Otherwise no source or a missing prerequisite skips it — or fails it, under
    ``DIAKTOROS_REQUIRE_HERMES_SOURCE=1`` (``hermes_prereqs.needs``, the CI verticals job).
    """
    def decorate(target):
        refusal = source_refusal()
        if refusal:
            if isinstance(target, type):
                def set_up_class(cls):
                    raise AssertionError(refusal)
                target.setUpClass = classmethod(set_up_class)
                return target

            @functools.wraps(target)
            def refuse(*args, **kwargs):
                raise AssertionError(refusal)
            return refuse
        ready = (HERMES_AGENT_SOURCE is not None
                 and (HERMES_AGENT_SOURCE / "venv/bin/hermes").exists() and all(prerequisites))
        from hermes_prereqs import needs
        return needs(ready, reason)(target)
    return decorate

# Create both homes now, in a fresh guarded process and in a guarded child alike: run_supervisor
# resolves HERMES_HOME strictly, so a guarded spawn must never depend on an earlier test (or the
# parent) having happened to create it. Never anywhere under a protected home (the same test as
# the shim's): a child that inherited one is an escape, and config.guard_real_home — which covers
# the whole real home, not only its .hermes — refuses it on first use (config.home(), a state dir,
# a ledger). The guard itself must not write there either.
for _home in {os.environ.get("HOME"), os.environ.get("HERMES_HOME")} - {None, ""}:
    _home = pathlib.Path(_home)
    if _home.is_absolute() and not _under_a_home(_home):
        _home.mkdir(parents=True, exist_ok=True)


RUST = (pathlib.Path(os.environ.get("RUSTUP_HOME") or USER_HOME / ".rustup")
        / "toolchains/stable-x86_64-unknown-linux-gnu")


# The run-ledger guard (#108): refuses a Supervisor whose ledger or presence marker is under the
# real ~/.hermes, as spelled or through a symlink. Last, once HOME/HERMES_HOME are pinned above.
import _ledger_guard  # noqa: E402,F401
