"""Experimental staged Hermes launcher, deliberately not a production backend.

Used by native vertical fixtures. The trusted caller stages credentialless code,
an export/working copy and live host capabilities. Production adoption remains
blocked on hard storage bounds and reliable parent/detached-child lifecycle.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from . import contained, seatbelt

PROVIDER = 'diaktoros-seatbelt-wire'


def run(*, code: Path, venv: Path, runtime: Path, rust: Path, home: Path,
        work: Path, export: Path, client: Path, scratch: Path, query: Path,
        inference_socket: Path, broker_socket: Path, model: str,
        sdk: Path | None = None, developer_tools: Path | None = None,
        dependencies: Path | None = None,
        timeout: int = 180, max_steps: int = 10):
    """Run native Hermes over Unix sockets; no automatic selection or real credentials.

    Read roots must be dedicated trusted runtime generations/snapshots. This is
    not a general API for mounting arbitrary operator directories or executing a
    live Hermes profile. Host socket directories remain outside writable roots.
    """
    reason = seatbelt.unavailable()
    if reason:
        raise contained.ContainmentUnavailable(reason)
    supplied = dict(code=code, venv=venv, runtime=runtime, rust=rust, home=home,
                    work=work, export=export, client=client, scratch=scratch,
                    query=query, inference_socket=inference_socket, broker_socket=broker_socket)
    paths = {name: Path(value).resolve(strict=True) for name, value in supplied.items()}
    code, venv, runtime, rust, home, work, export, client, scratch, query, inference_socket, broker_socket = (
        paths[name] for name in ('code', 'venv', 'runtime', 'rust', 'home', 'work', 'export',
                                'client', 'scratch', 'query', 'inference_socket', 'broker_socket'))
    if not model or timeout < 1 or not 1 <= max_steps <= 200:
        raise ValueError('invalid native turn limits/model')
    if code not in query.parents or not query.is_file():
        raise ValueError('query must be in the staged read-only code tree')
    build_roots = ()
    if any(p is not None for p in (sdk, developer_tools, dependencies)):
        if any(p is None for p in (sdk, developer_tools, dependencies)):
            raise ValueError('native builds require SDK, developer tools and vendored dependencies')
        sdk, developer_tools, dependencies = (Path(p).resolve(strict=True) for p in
                                               (sdk, developer_tools, dependencies))
        if not (developer_tools / 'bin/clang').is_file():
            raise ValueError('developer tools must name the selected compiler directory')
        build_roots = (sdk, developer_tools, dependencies)
    profile = seatbelt.profile(read_roots=(code, venv, runtime, rust, client, export, *build_roots),
                               write_roots=(home, work, scratch),
                               sockets=(inference_socket, broker_socket))
    plugin = home / 'plugins' / PROVIDER
    plugin.mkdir(parents=True, mode=0o700)
    shutil.copyfile(Path(__file__).with_name('seatbelt_wire.py'), plugin / '__init__.py')
    (plugin / 'plugin.yaml').write_text(
        f'name: {PROVIDER}\nkind: model-provider\nversion: 0.1.0\nmanifest_version: 2\n')
    (home / 'config.yaml').write_text(
        f'model:\n  provider: {PROVIDER}\n  default: {json.dumps(model)}\n'
        '  base_url: http://localhost/v1\n  api_key: sandbox-dummy\n'
        f'plugins:\n  enabled: [{PROVIDER}]\nmemory:\n  memory_enabled: false\n')
    env = {'HOME': str(home), 'HERMES_HOME': str(home),
           'USER': 'agent', 'LOGNAME': 'agent',
           'PATH': f'{venv}/bin:{rust}/bin:/usr/bin:/bin',
           'PYTHONPATH': f'{client}:{code}', 'PYTHONDONTWRITEBYTECODE': '1',
           'TMPDIR': str(scratch), 'CARGO_HOME': str(scratch / 'cargo'),
           'RUSTUP_HOME': str(scratch / 'rustup'), 'CARGO_TARGET_DIR': str(work / 'target'),
           'CARGO_NET_OFFLINE': 'true', 'GIT_CONFIG_GLOBAL': '/dev/null',
           # Cargo initializes LibreSSL even offline; never load host TLS configuration.
           'OPENSSL_CONF': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
           'OPENAI_API_KEY': 'sandbox-dummy',
           'DIAKTOROS_INFERENCE_SOCKET': str(inference_socket),
           'DIAKTOROS_BROKER_SOCKET': str(broker_socket), 'DIAKTOROS_WORK': str(work),
           'DIAKTOROS_EXPORT': str(export),
           'DIAKTOROS_TURN_FILE': str(client / 'review-loop-turn.json')}
    if build_roots:
        # Direct tools avoid rustup/xcrun proxies looking in the operator's profile.
        # Unit separators preserve SDK/compiler paths containing spaces.
        compiler = developer_tools / 'bin/clang'
        env.update(SDKROOT=str(sdk), CC=str(compiler),
                   AR=str(developer_tools / 'bin/ar'),
                   CARGO_ENCODED_RUSTFLAGS='\x1f'.join(
                       ('-C', f'linker={compiler}', '-C', 'link-arg=-isysroot',
                        '-C', f'link-arg={sdk}')))
        cargo_home = scratch / 'cargo'
        cargo_home.mkdir(mode=0o700)
        (cargo_home / 'config.toml').write_text(
            '[source.crates-io]\nreplace-with = "vendored"\n'
            f'[source.vendored]\ndirectory = {json.dumps(str(dependencies))}\n')
    entry = [str(venv / 'bin/python'), str(venv / 'bin/hermes'), 'chat',
             '--query-file', str(query), '--oneshot', '-Q', '--provider', PROVIDER,
             '-m', model, '-t', 'terminal,file', '--ignore-rules',
             '--max-turns', str(max_steps), '--run-budget', str(timeout)]
    # Profile file lives outside every writable root. A file avoids ARG_MAX limits.
    with tempfile.TemporaryDirectory(prefix='dk-policy-', dir='/tmp') as directory:
        policy = Path(directory) / 'profile.sb'
        policy.write_text(profile.text)
        policy.chmod(0o400)
        return contained.capture(profile.command(policy, entry), env=env,
                                 cwd=work, timeout=timeout + 30)
