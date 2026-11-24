"""Seatbelt profiles for the explicit opt-in native execution backend.

These profiles enforce filesystem and IPC restrictions on macOS. They supply no
mount namespace, disk quota, parent-death guarantee or detached-child supervisor.
Only trusted host code may choose roots, socket endpoints and the executable.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

SANDBOX_EXEC = Path('/usr/bin/sandbox-exec')


def unavailable() -> str:
    if sys.platform != 'darwin':
        return 'Seatbelt requires macOS'
    if not SANDBOX_EXEC.is_file():
        return 'macOS sandbox-exec is unavailable; no unsandboxed fallback'
    return ''


def _root(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('sandbox paths must be absolute')
    path = path.resolve(strict=True)
    if not path.is_dir() or path == Path('/'):
        raise ValueError('sandbox roots must be existing non-root directories')
    if any(ord(char) < 32 for char in str(path)):
        raise ValueError('sandbox paths contain control characters')
    return path


@dataclass(frozen=True)
class Profile:
    text: str
    parameters: tuple[tuple[str, str], ...]

    def command(self, policy_file: Path, entry: list[str], *,
                spawn_adapter: Path | None = None) -> list[str]:
        """Use a file and -D parameters, never interpolate paths into SBPL or a shell."""
        reason = unavailable()
        if reason:
            raise RuntimeError(reason)
        policy_file = Path(policy_file).resolve(strict=True)
        if not policy_file.is_file() or not entry or not Path(entry[0]).is_absolute():
            raise ValueError('a profile file and absolute executable are required')
        args = [str(SANDBOX_EXEC), '-f', str(policy_file)]
        for key, value in self.parameters:
            args.extend(['-D', f'{key}={value}'])
        if spawn_adapter is not None:
            adapter = Path(spawn_adapter)
            if not adapter.is_absolute():
                raise ValueError('spawn adapter must be an absolute library path')
            adapter = adapter.resolve(strict=True)
            if not adapter.is_file() or ':' in str(adapter) or any(ord(c) < 32 for c in str(adapter)):
                raise ValueError('spawn adapter must name one library file')
            # SIP strips DYLD_* before sandbox-exec runs; set it inside the sandbox.
            args.extend(['/usr/bin/env', f'DYLD_INSERT_LIBRARIES={adapter}'])
        return [*args, *entry]


def _socket_rules(sockets, writes, params, rules):
    if sockets:
        rules.append('(allow system-socket (socket-domain AF_UNIX))')
    for index, raw in enumerate(sockets):
        raw = Path(raw)
        if not raw.is_absolute() or raw.is_symlink():
            raise ValueError('socket endpoints must be absolute and not symlinks')
        path = raw.resolve(strict=True)
        if not path.is_socket() or any(ord(char) < 32 for char in str(path)):
            raise ValueError('a live Unix socket endpoint is required')
        if len(str(path).encode()) >= 104:
            raise ValueError('socket path exceeds the macOS sockaddr_un bound')
        if any(path == p or p in path.parents for p in writes):
            raise ValueError('capability sockets cannot be inside writable roots')
        key = f'SOCKET_{index}'
        params.append((key, str(path)))
        rules.append(f'(allow file-read* (literal (param "{key}")))')
        rules.append('(allow network-outbound'
                     f' (remote unix-socket (literal (param "{key}"))))')


def profile(*, read_roots: tuple[Path, ...], write_roots: tuple[Path, ...],
            sockets: tuple[Path, ...] = (), read_files: tuple[Path, ...] = ()) -> Profile:
    """Deny by default, allow narrow runtime/run roots and exact AF_UNIX endpoints.

    System executable/library trees are readable; home directories, /private,
    /Library and Homebrew prefixes are not granted wholesale. Runtime roots must
    name a dedicated interpreter/tool generation, not its containing user home.
    Socket permissions do not grant bind/listen or TCP/UDP access.
    """
    reads = tuple(_root(p) for p in read_roots)
    writes = tuple(_root(p) for p in write_roots)
    files = tuple(Path(p) for p in read_files)
    if any(not p.is_absolute() or not p.is_file() or any(ord(c) < 32 for c in str(p)) for p in files):
        raise ValueError('sandbox read files must be absolute existing files without control characters')
    if any(read.resolve() == write or write in read.resolve().parents
           for read in (*reads, *files) for write in writes):
        raise ValueError('read-only roots cannot be inside writable roots')
    params: list[tuple[str, str]] = []
    rules = ['(version 1)', '(deny default)',
             '(allow process-exec)', '(allow process-fork)',
             '(allow signal (target same-sandbox))',
             '(allow process-info* (target same-sandbox))',
             '(allow file-read* (subpath "/System") (subpath "/usr/lib")'
             ' (subpath "/usr/share") (subpath "/usr/bin") (subpath "/bin"))',
             '(allow file-read-metadata (literal "/") (literal "/private")'
             ' (literal "/private/tmp") (literal "/dev"))',
             # dyld reads the root directory during startup; literal is not recursive.
             '(allow file-read-data (literal "/"))',
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom")'
             ' (literal "/dev/random"))',
             '(allow file-write-data (literal "/dev/null"))',
             # macOS LibreSSL initializes this public system config even for offline Cargo.
             # Its library ignores OPENSSL_CONF; grant one file, never /etc or credential stores.
             '(allow file-read* (literal "/private/etc/ssl/openssl.cnf"))',
             '(allow sysctl-read (sysctl-name "hw.ncpu")'
             ' (sysctl-name "hw.activecpu") (sysctl-name "hw.logicalcpu")'
             ' (sysctl-name "hw.physicalcpu") (sysctl-name "hw.memsize")'
             ' (sysctl-name "hw.pagesize") (sysctl-name "hw.pagesize_compat")'
             ' (sysctl-name "hw.machine")'
             ' (sysctl-name "kern.osrelease") (sysctl-name "kern.ostype")'
             ' (sysctl-name "kern.osversion") (sysctl-name "kern.version")'
             ' (sysctl-name "kern.hostname")'
             ' (sysctl-name "kern.bootargs")'
             ' (sysctl-name "security.mac.lockdown_mode_state"))']
    for prefix, roots, operation, predicate in (('READ', reads, 'file-read*', 'subpath'),
                                                ('FILE', files, 'file-read*', 'literal'),
                                                ('WRITE', writes, 'file-read* file-write*', 'subpath')):
        for index, path in enumerate(roots):
            key = f'{prefix}_{index}'
            params.append((key, str(path)))
            rules.append(f'(allow {operation} ({predicate} (param "{key}")))')
            if prefix == 'FILE':
                # dyld must inspect versioned symlink names beside the exact library.
                key = f'FILE_PARENT_{index}'
                params.append((key, str(path.parent)))
                rules.append(f'(allow file-read-metadata (subpath (param "{key}")))')
            # Traversal/metadata only: never permit reading sibling file contents.
            for ancestor in path.parents:
                key = f'PARENT_{len(params)}'
                params.append((key, str(ancestor)))
                rules.append(f'(allow file-read-metadata (literal (param "{key}")))')
    _socket_rules(sockets, writes, params, rules)
    return Profile('\n'.join(rules) + '\n', tuple(params))
