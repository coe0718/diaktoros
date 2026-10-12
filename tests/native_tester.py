"""Experimental Mac tester entry point; no production backend or live credentials."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent.parent
SUITES = ('test_seatbelt.py', 'test_native_storage.py', 'test_native_snapshot.py',
          'test_native_lifecycle.py', 'test_native_roles.py', 'test_native_detach.py',
          'test_native_python_spawn.py', 'test_native_recovery.py', 'test_native_cleanup.py')


def environment():
    # Tests install _home_guard before importing the plugin. Keep only host
    # interpreter/tool discovery inputs; never inherit model/GitHub credentials
    # or plugin settings pointing at a live install.
    value = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
    for key in ('SEATBELT', 'NATIVE_STORAGE', 'NATIVE_LIFECYCLE', 'NATIVE_ROLES'):
        value['DIAKTOROS_REQUIRE_' + key] = '1'
    return value


def preflight():
    tools = ('/usr/bin/sandbox-exec', '/usr/bin/hdiutil', '/usr/sbin/diskutil',
             '/usr/bin/clang', '/usr/bin/xcrun')
    checks = {'macos': sys.platform == 'darwin', 'python_311_or_newer': sys.version_info >= (3, 11)}
    checks.update({path: Path(path).is_file() and os.access(path, os.X_OK) for path in tools})
    if checks['macos'] and checks['/usr/bin/xcrun']:
        try:
            result = subprocess.run(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'],
                                    env=environment(), capture_output=True, text=True, timeout=10)
            checks['apple_sdk'] = result.returncode == 0 and Path(result.stdout.strip()).is_dir()
        except (OSError, subprocess.TimeoutExpired):
            checks['apple_sdk'] = False
    else:
        checks['apple_sdk'] = False
    return {'architecture': platform.machine(), 'macos_version': platform.mac_ver()[0],
            'python': sys.version, 'checks': checks, 'passed': all(checks.values())}


def run_step(name, argv, output, *, timeout):
    started = time.monotonic()
    record = {'name': name, 'argv': argv, 'timeout_seconds': timeout, 'log': name + '.log'}
    with (output / record['log']).open('xb') as log:
        child = subprocess.Popen(argv, cwd=REPO, env=environment(), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            child.wait(timeout=timeout)
            record.update(returncode=child.returncode, status='passed' if child.returncode == 0 else 'failed')
        except BaseException as error:
            if child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=2)
            if not isinstance(error, subprocess.TimeoutExpired):
                raise
            record.update(returncode=child.returncode, status='timeout')
    record['seconds'] = round(time.monotonic() - started, 3)
    return record


def plan(mode, output):
    if mode == 'boundary':
        return [(Path(suite).stem, [sys.executable, 'tests/leakguard.py', 'discover', '-v',
                                  '-s', 'tests', '-p', suite], 240) for suite in SUITES]
    if mode == 'soak':
        return [('cleanup_precondition', [sys.executable, 'tests/leakguard.py', 'discover',
                                         '-v', '-s', 'tests', '-p', 'test_native_cleanup.py'], 240),
                ('cleanup_soak', [sys.executable, 'tests/native_cleanup_soak.py', '--turns', '200',
                                 '--output', str(output / 'soak')], 3600)]
    return []


def run(mode, output):
    output = Path(output).absolute()
    if output.parent.resolve(strict=True) != output.parent:
        raise ValueError('evidence parent must be canonical, without symlinks')
    output.mkdir(mode=0o700)  # Fresh evidence only; never overwrite a prior run.
    report = {'experimental': True, 'mode': mode, 'started': datetime.now(timezone.utc).isoformat(),
              'preflight': preflight(), 'steps': [], 'production_backend_enabled': False,
              'hermes_model_turns': False, 'physical_reboot_tested': False}
    def save():
        temporary = output / 'result.tmp'
        with temporary.open('w') as target:
            json.dump(report, target, indent=2)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(output / 'result.json')
    save()
    try:
        for name, argv in (('commit', ['/usr/bin/git', 'rev-parse', 'HEAD']),
                           ('working_tree', ['/usr/bin/git', 'status', '--porcelain'])):
            report['steps'].append(run_step(name, argv, output, timeout=10))
        if report['preflight']['passed'] and all(s['status'] == 'passed' for s in report['steps']):
            for name, argv, timeout in plan(mode, output):
                print(f'Running {name}; evidence: {output}', flush=True)
                report['steps'].append(run_step(name, argv, output, timeout=timeout))
                save()
                if report['steps'][-1]['status'] != 'passed':
                    break
        report['passed'] = report['preflight']['passed'] and all(s['status'] == 'passed' for s in report['steps'])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report.update(passed=False, error=str(error))
    finally:
        report['finished'] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({'passed': report['passed'], 'evidence': str(output), 'mode': mode}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('preflight', 'boundary', 'soak'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.mode, args.output))
