"""Native Seatbelt/APFS backend for the shared contained-command contract."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys
import tempfile

from . import contained, envnames, native_cleanup, native_lifecycle, native_recovery, native_shell, native_storage, seatbelt


def unavailable() -> str:
    if sys.platform != 'darwin':
        return 'native containment requires macOS'
    if envnames.get('NATIVE_MACOS') != '1':
        return 'native macOS is experimental; explicit opt-in requires DIAKTOROS_NATIVE_MACOS=1'
    for tool in ('/usr/bin/sandbox-exec', '/usr/bin/hdiutil', '/usr/sbin/diskutil',
                 '/usr/bin/clang', '/usr/bin/xcrun', '/usr/bin/codesign', '/usr/bin/lipo', '/usr/bin/otool'):
        if not os.access(tool, os.X_OK):
            return f'native containment requires {tool}; install Apple Command Line Tools'
    try:
        native_shell.source()
    except contained.ContainmentUnavailable as error:
        return str(error)
    return ''


@contextmanager
def workspace(*, pool: Path | None = None, size_mib: int | None = None):
    """One bounded volume; ownership exists before image creation."""
    if pool is None:
        from . import config
        pool = config.host_path('ledger').parent.resolve() / 'native-cleanup'
    pool = Path(pool)
    pool.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    size_mib = size_mib if size_mib is not None else (
        contained.CHECKOUT_SIZE + contained.SCRATCH_SIZE) // (1024 * 1024)
    if type(size_mib) is not int or not 64 <= size_mib <= 16384:
        raise contained.ContainmentUnavailable('native workspace must be 64..16384 MiB')
    policy = native_cleanup.Policy(max_pending_bytes=4 * size_mib * 1024 * 1024)
    manager = native_cleanup.Manager(pool, policy=policy)
    with native_storage.Workspace(size_mib, cleanup_manager=manager) as volume:
        try:
            yield volume
        finally:
            # The original preparation lease proves this live owner never armed a seat.
            if volume._preparation is not None:
                volume._preparation.preparation_failed()


def _copy(source: Path, destination: Path):
    if source != destination:
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def _translate(entry, mapping):
    """Translate only whole argv paths; never rewrite a shell or Python program."""
    result = []
    for arg in entry:
        for old, new in sorted(mapping.items(), key=lambda pair: len(pair[0]), reverse=True):
            if arg == old or arg.startswith(old + '/'):
                arg = str(new) + arg[len(old):]
                break
        result.append(arg)
    return result


def _tool_roots():
    from . import runtime_detect
    roots, reason = runtime_detect.native_tools()
    if reason:
        raise contained.ContainmentUnavailable(reason)
    return [roots['sdk'], roots['developer_tools']]


def _check_roots(paths, **additional):
    from . import runtime_detect
    for name, value in {**paths, **additional}.items():
        if name == 'query' or value is None:
            continue
        if reason := runtime_detect.native_root_problem(str(value)):
            raise contained.ContainmentUnavailable(f'unsafe native {name} root: {reason}')


def stage_shell(directory: Path) -> Path:
    """A private, ad-hoc signed shell keeps the adapter across terminal commands."""
    source = native_shell.source()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    shell = directory / 'bash'
    shutil.copyfile(source, shell)
    shell.chmod(0o700)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(shell)],
                   env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=30, check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', str(shell)],
                   env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=30, check=True)
    shell.chmod(0o500)
    return shell


def _layout(paths, volume, host, checkout_writable):
    staged_query = host / 'query'
    shutil.copyfile(paths['query'], staged_query)
    _copy(paths['home'], volume.home)
    _copy(paths['checkout'], volume.work)
    reads = [paths[k] for k in ('code', 'venv', 'runtime', 'rust', 'checkout')]
    mapping = {'/opt/code': paths['code'], '/opt/venv': paths['venv'],
               '/opt/rust': paths['rust'], '/opt/query': staged_query,
               str(paths['query']): staged_query,
               '/home/agent': volume.home, '/work': volume.work,
               '/tmp': volume.scratch, '/target': volume.scratch / 'target'}
    env = {'PATH': f"{paths['venv']}/bin:{paths['rust']}/bin:/usr/bin:/bin",
           'HOME': str(volume.home), 'HERMES_HOME': str(volume.home),
           'TMPDIR': str(volume.scratch), 'USER': 'agent', 'LOGNAME': 'agent',
           'PYTHONPATH': str(paths['code']), 'PYTHONDONTWRITEBYTECODE': '1',
           'CARGO_HOME': str(volume.scratch / 'cargo'),
           'RUSTUP_HOME': str(volume.scratch / 'rustup'),
           'CARGO_TARGET_DIR': str(volume.work / 'target' if checkout_writable
                                   else volume.scratch / 'target'),
           'CARGO_NET_OFFLINE': 'true', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'DIAKTOROS_WORK': str(volume.work), 'DIAKTOROS_EXPORT': str(paths['checkout'])}
    # CoreFoundation must not fall back to the denied host user-directory service.
    env['__CF_USER_TEXT_ENCODING'] = f'0x{os.getuid():X}:0:0'
    return reads, mapping, env


def _inputs(volume, reads, mapping, env, inference_socket_dir, broker_socket_dir,
            client_code, review_dir, node, dependency_caches):
    sockets = []
    for directory, filename, name, virtual in (
        (inference_socket_dir, 'model.sock', 'DIAKTOROS_INFERENCE_SOCKET', '/opt/inference'),
        (broker_socket_dir, 'broker.sock', 'DIAKTOROS_BROKER_SOCKET', '/run/review-loop/broker')):
        if directory is not None:
            directory = Path(directory).resolve(strict=True)
            endpoint = directory / filename
            sockets.append(endpoint)
            env[name] = str(endpoint)
            mapping[virtual] = directory
    if client_code is not None:
        client = Path(client_code).resolve(strict=True)
        reads.append(client)
        mapping['/opt/client'] = client
        env['PYTHONPATH'] = str(client) + os.pathsep + env['PYTHONPATH']
        env['DIAKTOROS_TURN_FILE'] = str(client / 'review-loop-turn.json')
    if review_dir is not None:
        review = Path(review_dir).resolve(strict=True)
        reads.append(review)
        mapping['/opt/review'] = review
    if node is not None:
        node = Path(node).resolve(strict=True)
        reads.append(node)
        mapping['/opt/node'] = node
        env['PATH'] = str(node / 'bin') + os.pathsep + env['PATH']
    for ecosystem, raw in (dependency_caches or {}).items():
        if ecosystem != 'rust':
            raise contained.ContainmentUnavailable('unsupported native dependency cache')
        cache = Path(raw).resolve(strict=True)
        reads.append(cache)
        cargo = volume.scratch / 'cargo'
        cargo.mkdir(mode=0o700, exist_ok=True)
        (cargo / 'registry').symlink_to(cache / 'registry', target_is_directory=True)
    return sockets


def _settings(reads, env, extra_env):
    tools = _tool_roots()
    reads.extend(tools)
    env.update(SDKROOT=str(tools[0]), CC=str(tools[1] / 'bin/clang'),
               AR=str(tools[1] / 'bin/ar'))
    env['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(
        ('-C', f'linker={tools[1] / "bin/clang"}', '-C', 'link-arg=-isysroot',
         '-C', f'link-arg={tools[0]}'))
    # Only trusted orchestration may add routing settings, never inherited credentials.
    if extra_env:
        allowed = {'DIAKTOROS_INFERENCE_SOCKET', 'DIAKTOROS_BROKER_SOCKET',
                   'DIAKTOROS_TURN_FILE', 'DIAKTOROS_WORK', 'DIAKTOROS_EXPORT',
                   'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'}
        if set(extra_env) - allowed or any(
                key.endswith('_API_KEY') and value != 'sandbox-dummy'
                for key, value in extra_env.items()):
            raise ValueError('native environment contains unsupported settings')
        env.update(extra_env)


def _launch(volume, host, reads, mapping, env, sockets, entry, checkout_writable, timeout, stop):
    shell = stage_shell(host / 'shell')
    shell_libraries = native_shell.read_files(shell)
    mapping.update({'/bin/sh': shell, '/bin/bash': shell})
    env['SHELL'] = str(shell)
    env['PATH'] = str(shell.parent) + os.pathsep + env['PATH']
    entry = _translate(entry, mapping)
    python = mapping['/opt/venv'] / 'bin/python'
    if Path(entry[0]) == python:
        from . import runtime_detect
        entry[0] = str(runtime_detect.native_python(mapping['/opt/venv']))
        env['__PYVENV_LAUNCHER__'] = str(python)
    adapter = host / 'spawn.dylib'
    architectures = ['-arch', 'arm64', '-arch', 'arm64e'] if platform.machine() == 'arm64' else ['-arch', 'x86_64']
    subprocess.run(['/usr/bin/clang', '-dynamiclib', '-Wall', '-Wextra', '-Werror',
                    '-Wno-deprecated-declarations', *architectures,
                    str(Path(__file__).with_name('native_spawn.c')), '-o', str(adapter)],
                   env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=60, check=True)
    reads.append(host)
    writes = [volume.home, volume.scratch]
    (writes if checkout_writable else reads).append(volume.work)
    profile = seatbelt.profile(read_roots=tuple(dict.fromkeys(reads)),
                               write_roots=tuple(writes), sockets=tuple(sockets),
                               read_files=shell_libraries)
    policy = host / 'policy.sb'
    policy.write_text(profile.text + '(allow syscall-unix)\n'
                      '(deny syscall-unix (syscall-number 82 147 244))\n')
    policy.chmod(0o400)
    command = profile.command(policy, entry, spawn_adapter=adapter)
    native_recovery.arm(volume)
    try:
        return native_lifecycle.capture(command, env=env, cwd=volume.work, timeout=timeout,
                                        recovery_root=volume.root, stop=stop)
    except native_lifecycle.StoragePending:
        volume.active = False
        raise
    except native_lifecycle.WatchdogLost:
        volume.active = False
        volume.root = None
        raise
    except native_lifecycle.CleanupIncomplete:
        volume.retain('native execution completion is unverified')
        raise


def run(*, code: Path, venv: Path, runtime: Path, home: Path, checkout: Path,
        rust: Path, query: Path, entry: list[str], timeout: int = 180,
        network: bool = False, inference_socket_dir: Path | None = None,
        broker_socket_dir: Path | None = None, client_code: Path | None = None,
        checkout_writable: bool = True, dependency_caches: dict[str, Path] | None = None,
        review_dir: Path | None = None, node: Path | None = None, stop=None,
        workspace=None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run with strict kernel restrictions and verified process-group completion.

    A supplied workspace belongs to the caller, which may inspect bounded output
    files before exiting its context. No unsandboxed or network-enabled fallback.
    """
    if network:
        raise contained.ContainmentUnavailable('native turns cannot enable direct network access')
    if reason := unavailable():
        raise contained.ContainmentUnavailable(reason)
    if type(timeout) is not int or timeout < 1 or not entry:
        raise ValueError('a positive timeout and command are required')
    if stop is not None and stop.is_set():
        raise contained.Stopped()
    paths = {name: Path(value).resolve(strict=True) for name, value in
             dict(code=code, venv=venv, runtime=runtime, home=home, checkout=checkout,
                  rust=rust, query=query).items()}
    _check_roots(paths, node=node, client=client_code, review=review_dir,
                 **{f'{key} cache': value for key, value in (dependency_caches or {}).items()})
    with ExitStack() as stack:
        from .native_backend import workspace as create_workspace
        volume = workspace if workspace is not None else stack.enter_context(create_workspace())
        volume.validate(home=volume.home, work=volume.work, scratch=volume.scratch)
        temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='dk-native-'))
        host = Path(temporary).resolve()
        reads, mapping, env = _layout(paths, volume, host, checkout_writable)
        sockets = _inputs(volume, reads, mapping, env, inference_socket_dir, broker_socket_dir,
                          client_code, review_dir, node, dependency_caches)
        _settings(reads, env, extra_env)
        return _launch(volume, host, reads, mapping, env, sockets, entry,
                       checkout_writable, timeout, stop)
