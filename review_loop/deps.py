"""Host-side dependency prefetch for the offline sandbox (issue #51).

The sandbox has no network, so a seat cannot download a single crate: without help every
``cargo build`` on a repo with dependencies fails, and a reviewer told to verify would request
changes on every round. Before launch, the trusted host fetches the dependencies a staged PR
head pins into a per-repository host cache; ``contained`` then mounts that cache **read-only**
and the seat builds offline. When the prefetch is impossible or fails, the turn still runs and the
seat is told plainly that dependencies are unavailable, so it judges by reading instead of treating
"could not build" as a finding.

Trust decision (see docs/issue-16-boundary.md, "Dependency prefetch"): the host never runs cargo
against the PR's own manifests. Everything the PR controls — ``Cargo.toml``, ``build.rs``,
``.cargo/config.toml``, ``rust-toolchain.toml``, ``[patch]`` and git dependencies — stays out of
the host process. The host only *reads* ``Cargo.lock`` as data (``tomllib``), accepts nothing but
crates.io packages named and versioned by strict patterns, and writes its own synthetic manifest
pinning exactly those ``name = version`` pairs. ``cargo fetch`` on that manifest downloads and
unpacks crates from crates.io and compiles or executes nothing from them. It runs from a private
empty directory outside any user config, with an environment built from scratch (no token, no
proxy or credential variables, a throwaway ``HOME``), the configured toolchain's own ``cargo`` and
``rustc`` (never a rustup proxy that would honour a toolchain override), a timeout and a bounded
output capture.

Bounded in bytes as well as packages (a PR chooses the lockfile, so it chooses how much the host
downloads): the whole cache is capped at ``cache_cap()`` — 2 GiB unless ``REVIEW_LOOP_CRATE_CACHE_GIB``
says otherwise — and the cap is enforced *while* cargo runs: the cache's disk usage is measured every
``POLL`` seconds and the fetch is killed the moment it passes the cap, then everything that fetch
added is removed. A cache that outgrew the cap across many PRs is retired (renamed, and deleted once
no running turn still holds it) and the fetch retried once into a fresh one, so one PR is never
blamed for what earlier PRs left behind. Fetches for one repository are serialized by a lock file,
and a turn holds a shared lock on the cache generation it mounts until its sandbox has exited.

Deliberate boundary: a git dependency, or a package from any registry other than crates.io, is never
fetched — the host fetches nothing it cannot name, and such a source is a URL the PR chose. The seat
is told so, and that it is not a defect of the PR. A planned opt-in for public GitHub sources
(anonymous, pinned) is issue #114.

Pluggable: ``ECOSYSTEMS`` maps a name to a prefetcher; ``contained.DEPENDENCY_MOUNTS`` fixes where
(and only where) that ecosystem's cache is mounted and which environment makes it offline. Only
Rust is implemented.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import fcntl
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

READY, UNAVAILABLE = "ready", "unavailable"

CRATES_IO = frozenset({"registry+https://github.com/rust-lang/crates.io-index",
                       "sparse+https://index.crates.io/"})
# The hosts an anonymous sparse crates.io fetch contacts: the index, and the download host that
# the index's own config.json names ("dl": "https://static.crates.io/crates"). Under the test
# guard these are the only hosts any test may reach, and only through ``_fetch`` (guard_registry).
CRATES_IO_HOSTS = frozenset({"index.crates.io", "static.crates.io"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
                      r"(-[0-9A-Za-z.-]{1,64})?(\+[0-9A-Za-z.-]{1,64})?\Z")
MAX_LOCKFILE = 4 * 1024 * 1024
MAX_PACKAGES = 4000
FETCH_TIMEOUT = 300            # the whole prefetch: waiting for another, fetching, a retry
MAX_OUTPUT = 64 * 1024
MiB = 1024 ** 2
GIB = 1024 ** 3
# The crate cache's byte cap: a host limit (disk), so an environment setting of the process that
# runs the supervisor, like the sandbox size caps; forwarded to the detached worker by
# ``run_supervisor.HOST_LIMIT_ENV``. Sized from a real lockfile: patchhive/attest's 224 crates.io
# crates are a 263 MB cache, so 2 GiB holds several generations of a large workspace.
CAP_ENV = "REVIEW_LOOP_CRATE_CACHE_GIB"
DEFAULT_CAP_GIB = 2
POLL = 0.2                     # how often the cache is measured while cargo runs
IN_USE = ".in-use"             # a turn's shared lock on the cache generation it mounts
FETCH_LOCK = "fetch.lock"      # serializes one repository's prefetches (in the deps directory)
RETIRED = "cargo.retired-"
LEDGER_MAX = 600               # the run ledger's dependencies line
STOPPED = "stopped"            # ``bounded_run``: the ``stop`` predicate ended the run


@dataclass(frozen=True)
class Prefetch:
    """One ecosystem's outcome: what the sandbox mounts and what the seat is told."""
    ecosystem: str
    status: str                       # READY or UNAVAILABLE
    reason: str                       # short, host-written; never raw tool output
    cache: Path | None = None         # host directory mounted read-only when READY
    detail: str = field(default="", compare=False)   # bounded tool output tail, for operators
    boundary: bool = False            # refused by policy (a source the host will not fetch)
    seconds: float = field(default=0.0, compare=False)            # how long the prefetch took
    hold: "_Hold | None" = field(default=None, compare=False, repr=False)  # see ``release``

    @property
    def ready(self) -> bool:
        return self.status == READY


class _Hold:
    """A turn's shared lock on the cache generation it mounts; closing it is idempotent."""

    def __init__(self, fd: int):
        self.fd: int | None = fd

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _cap_setting() -> tuple[int, str | None]:
    raw = os.environ.get(CAP_ENV, "").strip()
    if not raw:
        return DEFAULT_CAP_GIB, None
    try:
        value = int(raw)
        if not 1 <= value <= 1024:
            raise ValueError("outside 1..1024")
    except ValueError as exc:
        return DEFAULT_CAP_GIB, (f"{CAP_ENV}={raw!r} ignored ({exc}); using "
                                 f"{DEFAULT_CAP_GIB} GiB")
    return value, None


def cache_cap() -> int:
    """The crate cache's byte cap: ``REVIEW_LOOP_CRATE_CACHE_GIB`` (1-1024), else 2 GiB.

    Read at each prefetch. A value nobody can parse keeps the default rather than taking an
    unattended loop down, and ``refused_cap_override`` names it so selftest can say so.
    """
    return _cap_setting()[0] * GIB


def refused_cap_override() -> str | None:
    return _cap_setting()[1]


def human_bytes(value: int) -> str:
    """A size in the largest binary unit it reaches, to at most two decimals ("16 KiB",
    "263.21 MiB", "2 GiB"); under 1 KiB, in bytes."""
    value = int(value)
    for unit, name in ((GIB, "GiB"), (MiB, "MiB"), (1024, "KiB")):
        if value >= unit:
            return f"{value / unit:.2f}".rstrip("0").rstrip(".") + f" {name}"
    return f"{value} byte{'' if value == 1 else 's'}"


def hold(cache: Path) -> int:
    """Take a shared lock on a cache generation, for as long as a turn may read it."""
    fd = os.open(Path(cache) / IN_USE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    return fd


def release(results: list[Prefetch]) -> None:
    """Drop the turn's holds once its sandbox has exited (safe to call more than once)."""
    for result in results:
        if result.hold is not None:
            result.hold.close()


def _held(generation: Path) -> bool:
    """Whether any turn still holds this cache generation (a shared ``IN_USE`` lock)."""
    try:
        fd = os.open(generation / IN_USE, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _usage(root: Path) -> int:
    """Disk bytes under ``root`` (allocated blocks, like ``du``); symlinks are not followed."""
    total, stack = 0, [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        total += entry.stat(follow_symlinks=False).st_blocks * 512
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except FileNotFoundError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
    return total


def _paths(root: Path) -> set[str]:
    found, stack = set(), [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    found.add(os.path.relpath(entry.path, root))
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except (FileNotFoundError, NotADirectoryError):
            continue
    return found


def _rmtree(path: Path) -> None:
    def force(_func, target, _exc):
        try:
            os.chmod(os.path.dirname(target), 0o700)
            os.chmod(target, 0o700)
        except OSError:
            pass
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            try:
                os.unlink(target)
            except OSError:
                pass
    shutil.rmtree(path, onerror=force)


def _discard_new(root: Path, before: set[str]) -> None:
    """Remove everything under ``root`` that was not there before: what a stopped fetch added."""
    removed = None
    for relative in sorted(_paths(root) - before):
        if removed is not None and relative.startswith(removed + os.sep):
            continue
        path = root / relative
        if path.is_dir() and not path.is_symlink():
            _rmtree(path)
            removed = relative
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _has_crates(cache: Path) -> bool:
    """Whether a cache holds any downloaded or unpacked crate (anything a retry would not)."""
    for kind in ("cache", "src"):
        base = cache / "registry" / kind
        if base.is_dir() and any(any(index.iterdir()) for index in base.iterdir()
                                 if index.is_dir() and not index.is_symlink()):
            return True
    return False


def _sweep(parent: Path) -> None:
    """Delete retired cache generations no running turn holds any more."""
    for generation in parent.glob(RETIRED + "*"):
        if generation.is_dir() and not generation.is_symlink() and not _held(generation):
            _rmtree(generation)


def _lock(path: Path, deadline: float) -> int | None:
    """An exclusive lock on ``path``, waiting no later than ``deadline``; None if it never came."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.2)


def cache_root(loop: dict) -> Path:
    """The per-repository host cache: private, under the loop's own state directory."""
    from .config import state_dir
    root = state_dir(loop) / "deps"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise PermissionError(f"{root} must be a private (0700) directory")
    return root


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(limit + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > limit:
        raise ValueError("too large")
    return data


class SourceRefused(ValueError):
    """The lockfile names a source the host will not contact: the deliberate boundary."""


def locked_crates(lockfile: Path) -> list[tuple[str, str]]:
    """The crates.io ``(name, version)`` pairs a ``Cargo.lock`` pins; refuse anything else.

    Path/workspace packages (no ``source``) are skipped: they are the repository itself. A git
    source or any other registry is refused, because fetching it means the host contacting a
    URL the PR chose.
    """
    import tomllib
    try:
        document = tomllib.loads(_read_regular(lockfile, MAX_LOCKFILE).decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("Cargo.lock is unreadable or not valid TOML") from exc
    packages = document.get("package", [])
    if not isinstance(packages, list) or len(packages) > MAX_PACKAGES:
        raise ValueError(f"Cargo.lock lists more than {MAX_PACKAGES} packages or is malformed")
    crates: set[tuple[str, str]] = set()
    git = other = 0
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("Cargo.lock has a malformed package entry")
        source = package.get("source")
        if source is None:
            continue
        if source not in CRATES_IO:
            # Counted, never named: a crate name or URL is PR-chosen text, and this reason is
            # written into the seat's host note and the run ledger.
            if isinstance(source, str) and source.startswith("git+"):
                git += 1
            else:
                other += 1
            continue
        name, version = package.get("name"), package.get("version")
        if not (isinstance(name, str) and _NAME.match(name) and isinstance(version, str)
                and _VERSION.match(version)):
            raise ValueError("Cargo.lock has a package name or version outside the accepted form")
        crates.add((name, version))
    if git or other:
        what = [f"{git} git dependenc{'y' if git == 1 else 'ies'}"] if git else []
        if other:
            what.append(f"{other} package{'' if other == 1 else 's'} from a registry other than "
                        "crates.io")
        raise SourceRefused(f"Cargo.lock pins {' and '.join(what)} (non-crates.io sources the "
                            "host does not fetch)")
    return sorted(crates)


def synthetic_manifest(crates: list[tuple[str, str]]) -> str:
    """A throwaway package depending on exactly each locked crate at its exact version."""
    lines = ['[package]', 'name = "review-loop-prefetch"', 'version = "0.0.0"',
             'edition = "2021"', 'publish = false', '', '[dependencies]']
    for index, (name, version) in enumerate(crates):
        lines.append(f'd{index} = {{ package = "{name}", version = "={version}", '
                     'default-features = false }')
    return "\n".join(lines) + "\n"


def cargo_env(home: Path, cache: Path, rustc: Path, cargo: Path) -> dict:
    """The fetch's whole environment, built from scratch: nothing the PR or the host set leaks in."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "CARGO_HOME": str(cache),
            "RUSTC": str(rustc), "CARGO": str(cargo), "CARGO_TERM_COLOR": "never",
            "CARGO_TERM_PROGRESS_WHEN": "never", "CARGO_NET_RETRY": "2",
            "CARGO_HTTP_TIMEOUT": "60", "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": "sparse",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "LANG": "C.UTF-8"}


_CARGO_ENV_KEYS = frozenset(cargo_env(Path("/"), Path("/"), Path("/"), Path("/")))


def guard_registry(env: dict, work: Path, cache: Path, manifest: str, locked: bytes) -> None:
    """Under the test guard, refuse a fetch that could reach anything but crates.io's own hosts.

    The prefetch is the one network path the test guard allows: an anonymous, credential-free
    fetch from ``CRATES_IO_HOSTS``. cargo is a subprocess, so this checks its effective registry
    configuration before it runs, not its sockets: the environment is exactly ``cargo_env`` (no
    proxy, no other registry, no source replacement) with the sparse crates.io index; no cargo
    config file can apply (the package's ancestors, ``CARGO_HOME``, the scratch ``HOME``); the
    manifest names no registry, source or patch; and every lockfile source is crates.io.
    Outside the guard it does nothing: production already builds exactly this configuration.
    """
    from .config import _ARMED, RealNetworkError, test_guard_active
    if not test_guard_active():
        return
    why = []
    proxies = sorted(name for name in env if "proxy" in name.lower())
    if proxies:
        why.append(f"a proxy is configured ({', '.join(proxies)})")
    extra = sorted(set(env) - _CARGO_ENV_KEYS - set(proxies))
    if extra:
        why.append(f"the environment sets {', '.join(extra)} (another registry or a source "
                   "replacement)")
    if env.get("CARGO_REGISTRIES_CRATES_IO_PROTOCOL") != "sparse":
        why.append("crates.io must be read through its sparse index (index.crates.io)")
    configs = [str(path) for base in (work, *work.parents)
               for path in (base / ".cargo/config.toml", base / ".cargo/config") if path.exists()]
    configs += [str(path) for path in (cache / "config.toml", cache / "config",
                                       Path(env.get("HOME", "/nonexistent")) / ".cargo/config.toml")
                if path.exists()]
    if configs:
        why.append(f"a cargo config file could redirect it ({', '.join(configs)})")
    if re.search(r"\bregistry\b|^\s*\[(source|registries|patch|replace)\b", manifest, re.M):
        why.append("the manifest names a registry, source or patch")
    sources = set(re.findall(r'^source = "([^"]*)"', locked.decode("utf-8", "replace"), re.M))
    if sources - CRATES_IO:
        why.append(f"the lockfile names other sources ({', '.join(sorted(sources - CRATES_IO))})")
    if why:
        raise RealNetworkError(
            f"{_ARMED}. Otherwise: the only network fetch allowed under the test guard is an "
            f"anonymous crates.io fetch ({', '.join(sorted(CRATES_IO_HOSTS))}), and this one "
            f"could go elsewhere: {'; '.join(why)}")


def bounded_run(argv: list[str], *, env: dict, cwd: Path, timeout: float,
                limit: int = MAX_OUTPUT, stop=None,
                poll: float = POLL) -> tuple[int | str | None, str]:
    """Run with a scratch environment; keep the last ``limit`` bytes; kill the group on timeout.

    Returns ``(returncode, tail)``; ``returncode`` is ``None`` when the deadline passed, and
    ``STOPPED`` when ``stop()`` (checked about every ``poll`` seconds while it runs) said to end it.
    A slow ``stop`` is checked less often, never more than a third of the time.
    """
    process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    check = time.monotonic() + poll
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    return None, output[-limit:].decode(errors="replace")
                if stop is not None and now >= check:
                    if stop():
                        return STOPPED, output[-limit:].decode(errors="replace")
                    spent = time.monotonic() - now
                    check = time.monotonic() + max(poll, 3 * spent)
                wait = remaining if stop is None else max(min(remaining, check - now), 0.01)
                for key, _ in selector.select(wait):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    del output[:-limit]
        try:
            return process.wait(timeout=max(deadline - time.monotonic(), 0.1)), \
                output[-limit:].decode(errors="replace")
        except subprocess.TimeoutExpired:
            return None, output[-limit:].decode(errors="replace")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def _cached(cache: Path, crates: list[tuple[str, str]]) -> list[str]:
    """Locked crates with no downloaded ``.crate`` in the crates.io cache."""
    have: set[str] = set()
    registry = cache / "registry" / "cache"
    if registry.is_dir():
        for index in registry.iterdir():
            if index.name.startswith("index.crates.io-") and index.is_dir():
                have.update(entry.name for entry in index.iterdir())
    return [f"{name} {version}" for name, version in crates
            if f"{name}-{version}.crate" not in have]


def _fetch(cache: Path, crates: list[tuple[str, str]], locked: bytes, cargo: Path,
           rustc: Path, deadline: float, cap: int, timeout: float) -> Prefetch | str:
    """One ``cargo fetch`` into ``cache``: a READY/UNAVAILABLE outcome, or ``STOPPED`` at the cap.

    A fetch stopped at the cap or the deadline leaves nothing behind: whatever it added to the
    cache is removed before this returns.
    """
    before = _paths(cache)
    if _usage(cache) > cap:
        return STOPPED
    with tempfile.TemporaryDirectory(prefix="rl-prefetch-") as tmp:
        work, home = Path(tmp) / "pkg", Path(tmp) / "home"
        (work / "src").mkdir(parents=True)
        home.mkdir()
        (work / "src" / "lib.rs").write_text("")
        manifest = synthetic_manifest(crates)
        (work / "Cargo.toml").write_text(manifest)
        # Seed the resolver with the PR's lockfile *as data*: it keeps each locked version even if
        # it was yanked since. Cargo re-derives the root; this file never reaches the sandbox.
        (work / "Cargo.lock").write_bytes(locked)
        env = cargo_env(home, cache, rustc, cargo)
        # Under the test guard, the one allowed network path — and only to crates.io's hosts.
        guard_registry(env, work, cache, manifest, locked)
        try:
            rc, tail = bounded_run([str(cargo), "fetch"], env=env, cwd=work,
                                   timeout=max(deadline - time.monotonic(), 0.1),
                                   stop=lambda: _usage(cache) > cap)
        except OSError as exc:
            return Prefetch("rust", UNAVAILABLE, f"cargo could not start ({type(exc).__name__})")
    # The last check catches what landed after the final poll (a fetch that ended over the cap).
    if rc is STOPPED or (rc is not None and _usage(cache) > cap):
        _discard_new(cache, before)
        return STOPPED
    if rc is None:
        _discard_new(cache, before)
        return Prefetch("rust", UNAVAILABLE, f"the host fetch timed out after {int(timeout)}s",
                        detail=tail)
    missing = _cached(cache, crates)
    if rc != 0 or missing:
        what = f"{len(missing)} of {len(crates)} crates missing" if missing else f"rc={rc}"
        return Prefetch("rust", UNAVAILABLE, f"the host fetch from crates.io failed ({what})",
                        detail=tail)
    return Prefetch("rust", READY, f"{len(crates)} crates.io crates from Cargo.lock", cache)


def _over(cap: int, why: str = "") -> Prefetch:
    return Prefetch("rust", UNAVAILABLE, f"the host crate cache would exceed {human_bytes(cap)} "
                    f"(host limit {CAP_ENV}){why}")


def prefetch_rust(checkout: Path, cache_parent: Path | None, rust: Path,
                  timeout: float = FETCH_TIMEOUT, cap: int | None = None) -> Prefetch | None:
    """Fetch a staged Rust head's crates.io dependencies into ``cache_parent/cargo``.

    ``None`` when the head is not a Rust project. Never raises for a PR-controlled reason: an
    unusable lockfile, a refused source, a failed fetch or one past the byte cap (``cap``, default
    ``cache_cap()``) is an ``UNAVAILABLE`` outcome with a plain reason. A READY outcome carries a
    shared hold on the cache it names; the caller ``release``s it after the sandbox exits.
    ``timeout`` bounds the whole call, including waiting for another prefetch of this repository.
    """
    deadline = time.monotonic() + timeout
    checkout = Path(checkout)
    lockfile, manifest = checkout / "Cargo.lock", checkout / "Cargo.toml"
    if not lockfile.exists() and not manifest.exists():
        return None
    if not lockfile.is_file() or lockfile.is_symlink():
        return Prefetch("rust", UNAVAILABLE, "the head has no Cargo.lock at its root, so there is "
                        "nothing pinned the host could fetch")
    try:
        crates = locked_crates(lockfile)
        locked = _read_regular(lockfile, MAX_LOCKFILE)
    except SourceRefused as exc:
        return Prefetch("rust", UNAVAILABLE, str(exc), boundary=True)
    except (OSError, ValueError) as exc:
        return Prefetch("rust", UNAVAILABLE, str(exc) if isinstance(exc, ValueError)
                        else "Cargo.lock is unreadable")
    if cache_parent is None:
        return Prefetch("rust", UNAVAILABLE, "the host dependency cache is unusable (it must be "
                        "a private 0700 directory under the loop's state_dir)")
    cap = cache_cap() if cap is None else cap
    cargo, rustc = Path(rust) / "bin" / "cargo", Path(rust) / "bin" / "rustc"
    if crates and not (cargo.is_file() and rustc.is_file()):
        return Prefetch("rust", UNAVAILABLE, "the configured Rust toolchain has no cargo/rustc")
    parent = Path(cache_parent)
    lock = _lock(parent / FETCH_LOCK, deadline)
    if lock is None:
        return Prefetch("rust", UNAVAILABLE, "another prefetch for this repository did not finish "
                        f"within {int(timeout)}s")
    try:
        _sweep(parent)
        cache = parent / "cargo"
        cache.mkdir(mode=0o700, exist_ok=True)
        if cache.is_symlink() or not cache.is_dir():
            return Prefetch("rust", UNAVAILABLE, "the host crate cache is not a private directory")
        (cache / "registry").mkdir(mode=0o700, exist_ok=True)
        if not crates:
            return Prefetch("rust", READY, "Cargo.lock pins no crates.io dependencies", cache,
                            hold=_Hold(hold(cache)))
        outcome = _fetch(cache, crates, locked, cargo, rustc, deadline, cap, timeout)
        if outcome is STOPPED and _has_crates(cache):
            # Over the cap with crates earlier PRs left: retire that generation (a running turn
            # keeps reading it; it is deleted once none holds it) and retry once from empty.
            if any(parent.glob(RETIRED + "*")):
                return _over(cap, "; the previous cache is still in use by a running turn, so it "
                                  "cannot be replaced yet")
            retired = parent / f"{RETIRED}{time.time_ns()}"
            cache.rename(retired)
            cache.mkdir(mode=0o700)
            (cache / "registry").mkdir(mode=0o700)
            outcome = _fetch(cache, crates, locked, cargo, rustc, deadline, cap, timeout)
            if outcome is STOPPED or not outcome.ready:
                # This lockfile alone does not fit (or the retry failed): put the old one back.
                _rmtree(cache)
                retired.rename(cache)
        if outcome is STOPPED:
            return _over(cap)
        if outcome.ready:
            outcome = replace(outcome, hold=_Hold(hold(cache)))
        _sweep(parent)
        return outcome
    finally:
        os.close(lock)


ECOSYSTEMS = {"rust": prefetch_rust}


def prepare(checkout: Path, cache_parent: Path | None, rust: Path,
            timeout: float = FETCH_TIMEOUT) -> list[Prefetch]:
    """Run every applicable prefetcher; a crash in one is an UNAVAILABLE outcome, not a raise.

    ``cache_parent`` is ``None`` when the host cache is unusable: nothing is fetched, and a
    project that needed it is reported UNAVAILABLE rather than failing the turn. Each outcome
    records how long it took; READY ones hold their cache until ``release``.
    """
    results = []
    for name, prefetcher in ECOSYSTEMS.items():
        started = time.monotonic()
        try:
            outcome = prefetcher(checkout, cache_parent, rust, timeout)
        except Exception as exc:  # the turn still runs; the seat is told
            outcome = Prefetch(name, UNAVAILABLE, f"host prefetch error ({type(exc).__name__})")
        if outcome is not None:
            results.append(replace(outcome, seconds=time.monotonic() - started))
    return results


def _line(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def ledger_text(results: list[Prefetch]) -> str:
    """One bounded line for the run ledger: each ecosystem's status and host-written reason.

    Never tool output (``detail`` stays with the operator's selftest), so nothing a download
    printed — and no credential, since the fetch has none — reaches the ledger.
    """
    if not results:
        return "none — no dependency lockfile at the head's root"
    text = "; ".join(f"{r.ecosystem}: {r.status} — {r.reason} ({r.seconds:.1f}s)"
                     for r in results)
    text = _line(text)
    return text if len(text) <= LEDGER_MAX else text[:LEDGER_MAX - 1] + "…"


_LABEL = {"rust": ("Rust", "crates", "`cargo build`, `cargo test` and `cargo metadata`")}


def seat_note(results: list[Prefetch], role: str) -> str:
    """What the seat is told about building, appended to its query by the host."""
    if not results:
        return ""
    lines = ["Build environment (host-checked before this turn):"]
    for result in results:
        label, unit, commands = _LABEL.get(result.ecosystem, (result.ecosystem, "dependencies",
                                                             "builds"))
        if result.ready:
            lines.append(f"- {label}: dependencies are available offline ({result.reason}); the "
                         f"{unit} cache is read-only and the network is off, so {commands} work "
                         "as long as they need nothing beyond Cargo.lock.")
            continue
        lines.append(f"- {label}: dependencies are NOT available in this sandbox — {result.reason}. "
                     f"{commands} will fail on missing {unit}; that is the sandbox, not the PR.")
        if result.boundary:
            lines.append("  This is a deliberate host boundary, not a failure: the host fetches "
                         "nothing it cannot name. A git dependency or another registry is a URL "
                         "the PR chose, so the host never contacts it, and the sandbox has no "
                         "network. Needing one is not a defect of the PR; only what the code "
                         "itself shows is.")
        if role == "fixer":
            lines.append("  You cannot build or test your fix here: check it by reading, and say "
                         "in your published answers that it is unbuilt.")
        elif role == "adjudicator":
            lines.append("  Neither seat could build either. Rule on what can be shown from the "
                         "code; a finding or answer that rests only on the missing build is not "
                         "evidence either way.")
        else:
            lines.append("  Judge by reading the code and the diff. That the build could not run "
                         "is not by itself a finding and not by itself a reason to request "
                         "changes: say plainly in your review what you could not run, and base "
                         "the verdict on what you can show from the code.")
    return "\n".join(lines)
