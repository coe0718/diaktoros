"""Stage the same pinned, fake-provider Hermes/Rust vertical used by native CI."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from native_tester import REPO, environment, preflight, run_step

HERMES_COMMIT = 'be5e9f72c6681af9dfb75bf480f08844f1499949'
RUST_VERSION = '1.85.1'
ENV_PATHS = ('HERMES_AGENT_SOURCE', 'DIAKTOROS_NATIVE_RUST_ROOT',
             'DIAKTOROS_NATIVE_BUILD_FIXTURE', 'DIAKTOROS_NATIVE_SDK',
             'DIAKTOROS_NATIVE_DEVELOPER_TOOLS', 'DIAKTOROS_NATIVE_SPAWN_ADAPTER')


def prepared_environment(manifest):
    if manifest is None:
        raise ValueError('prepared Hermes manifest required')
    manifest = Path(manifest).resolve(strict=True)
    if manifest.stat().st_size > 65536:
        raise ValueError('prepared Hermes manifest exceeds bound')
    value = json.loads(manifest.read_text())
    if (not isinstance(value, dict) or value.get('passed') is not True or
            value.get('hermes_commit') != HERMES_COMMIT):
        raise ValueError('Hermes preparation incomplete or different pinned commit')
    paths = value.get('paths')
    if not isinstance(paths, dict) or any(type(paths.get(key)) is not str or not paths[key] for key in ENV_PATHS):
        raise ValueError('prepared Hermes paths missing or invalid')
    env = {}
    for key in ENV_PATHS:
        path = Path(value['paths'][key]).resolve(strict=True)
        env[key] = str(path)
    source = Path(env['HERMES_AGENT_SOURCE'])
    # Operator-provided fixtures must never be the live Hermes checkout.
    real_home = Path.home().resolve()
    if source == (real_home/'.hermes/hermes-agent').resolve() or (real_home/'.hermes').resolve() in source.parents:
        raise ValueError('a disposable Hermes checkout is required')
    commit = subprocess.run(['/usr/bin/git', '-C', str(source), 'rev-parse', 'HEAD'],
                            env=environment(), capture_output=True, text=True, timeout=10, check=True)
    if commit.stdout.strip() != HERMES_COMMIT or not (source/'venv/bin/hermes').is_file():
        raise ValueError('prepared Hermes source or executable does not match pinned fixture')
    if not (Path(env['DIAKTOROS_NATIVE_BUILD_FIXTURE'])/'Cargo.lock').is_file():
        raise ValueError('prepared Rust fixture lockfile missing')
    for key, suffix in (('DIAKTOROS_NATIVE_RUST_ROOT', 'bin/rustc'),
                        ('DIAKTOROS_NATIVE_RUST_ROOT', 'bin/cargo'),
                        ('DIAKTOROS_NATIVE_BUILD_FIXTURE', 'vendor/memchr/Cargo.toml'),
                        ('DIAKTOROS_NATIVE_DEVELOPER_TOOLS', 'bin/clang')):
        if not (Path(env[key])/suffix).is_file():
            raise ValueError('prepared tool or vendor input missing: ' + key + '/' + suffix)
    if not Path(env['DIAKTOROS_NATIVE_SPAWN_ADAPTER']).is_file() or not Path(env['DIAKTOROS_NATIVE_SDK']).is_dir():
        raise ValueError('prepared spawn adapter or Apple SDK missing')
    env['DIAKTOROS_REQUIRE_NATIVE_HERMES'] = '1'
    return env


def prepare(output):
    output = Path(output).absolute()
    if output.parent.resolve(strict=True) != output.parent:
        raise ValueError('preparation parent must be canonical, without symlinks')
    output.mkdir(mode=0o700)
    value = {'experimental': True, 'hermes_commit': HERMES_COMMIT, 'rust_version': RUST_VERSION,
             'passed': False, 'steps': [], 'paths': {}, 'preflight': preflight()}
    def save():
        temporary = output/'result.tmp'
        temporary.write_text(json.dumps(value, indent=2))
        temporary.replace(output/'result.json')
    def step(name, argv, *, timeout=300, env=None, cwd=REPO):
        result = run_step(name, argv, output, timeout=timeout, extra_env=env, cwd=cwd)
        value['steps'].append(result)
        save()
        if result['status'] != 'passed':
            raise ValueError(f'{name} failed; see {result["log"]}')
        return (output/result['log']).read_text().strip().splitlines()[-1:]
    save()
    try:
        if not value['preflight']['passed']:
            raise ValueError('native preflight failed')
        uv, rustup = shutil.which('uv'), shutil.which('rustup')
        if not uv or not rustup:
            raise ValueError('uv and rustup are required; preparation did not install global tools')
        uv_version = step('uv_version', [uv, '--version'])
        if not uv_version or uv_version[0].split()[:2] != ['uv', '0.12.19']:
            raise ValueError('this fixture requires the CI-pinned uv 0.12.19')
        source, fixture = output/'hermes', output/'build-fixture'
        git_env = {'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':'/dev/null', 'GIT_TERMINAL_PROMPT':'0'}
        step('hermes_clone', ['/usr/bin/git', 'clone', '--no-checkout',
                             'https://github.com/NousResearch/hermes-agent.git', str(source)], timeout=600, env=git_env)
        step('hermes_checkout', ['/usr/bin/git', '-C', str(source), 'checkout', '--detach', HERMES_COMMIT], env=git_env)
        python_version = json.loads((source/'pm/lock.json').read_text())['packages']['python']['version'].split('+')[0]
        value['python_version'] = python_version
        rust_env = {'RUSTUP_HOME': str(output/'rustup'), 'CARGO_HOME': str(output/'cargo')}
        step('rust_install', [rustup, 'toolchain', 'install', RUST_VERSION, '--profile', 'minimal', '--no-self-update'],
             timeout=900, env=rust_env)
        lines = step('rust_root', [rustup, 'run', RUST_VERSION, 'rustc', '--print', 'sysroot'], env=rust_env)
        rust = Path(lines[-1]).resolve(strict=True)
        rust_env['PATH'] = str(rust/'bin') + os.pathsep + environment().get('PATH', '/usr/bin:/bin')
        step('rust_fixture', [sys.executable, 'tests/native_rust_fixture.py', str(fixture)])
        step('rust_lock', [str(rust/'bin/cargo'), 'generate-lockfile', '--manifest-path', str(fixture/'Cargo.toml')], env=rust_env)
        step('rust_vendor', [str(rust/'bin/cargo'), 'vendor', '--locked', '--manifest-path', str(fixture/'Cargo.toml'),
                             str(fixture/'vendor')], env=rust_env)
        sdk = Path(step('apple_sdk', ['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'])[-1]).resolve(strict=True)
        clang = Path(step('apple_clang', ['/usr/bin/xcrun', '--sdk', 'macosx', '--find', 'clang'])[-1]).resolve(strict=True)
        uv_env = {'UV_PROJECT_ENVIRONMENT': str(source/'venv'), 'UV_PYTHON_PREFERENCE':'only-managed',
                  'UV_CACHE_DIR': str(output/'uv-cache'), 'UV_PYTHON_INSTALL_DIR': str(output/'uv-python'),
                  'UV_NO_CONFIG':'1'}
        step('hermes_venv', [uv, 'venv', '--python', python_version, str(source/'venv')],
             timeout=600, env=uv_env)
        step('hermes_install', [uv, 'sync', '--frozen', '--no-dev', '--extra', 'anthropic'], timeout=1200,
             env=uv_env, cwd=source)
        adapter = output/'spawn.dylib'
        arches = ['-arch', 'arm64', '-arch', 'arm64e'] if platform.machine() == 'arm64' else ['-arch', 'x86_64']
        step('spawn_adapter', ['/usr/bin/clang', '-dynamiclib', '-Wall', '-Wextra', '-Werror',
                              '-Wno-deprecated-declarations', *arches, str(REPO/'diaktoros/native_spawn.c'),
                              '-o', str(adapter)])
        value['paths'] = dict(zip(ENV_PATHS, map(str, (source, rust, fixture, sdk, clang.parent.parent, adapter))))
        value['passed'] = True
        save()
        prepared_environment(output/'result.json')
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError) as error:
        value.update(passed=False, error=str(error))
    finally:
        save()
    print(json.dumps({'passed':value['passed'], 'manifest':str(output/'result.json')}), flush=True)
    return 0 if value['passed'] else 1
