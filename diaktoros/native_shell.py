"""Select a re-signable native Bash and its exact read-only library files."""
from pathlib import Path
import os
import platform
import shutil
import subprocess

from . import contained, runtime_detect


def _checked_root(path: Path) -> Path:
    if reason := runtime_detect.native_root_problem(str(path)):
        raise contained.ContainmentUnavailable(f'unsafe native shell library root: {reason}')
    return path


def read_files(executable: Path) -> tuple[Path, ...]:
    """Follow absolute Mach-O dependencies, keeping aliases and canonical files."""
    pending, seen, files = [executable.resolve(strict=True)], set(), set()
    while pending:
        binary = pending.pop()
        if binary in seen:
            continue
        seen.add(binary)
        if len(seen) > 64:
            raise contained.ContainmentUnavailable('native shell library graph exceeds 64 files')
        try:
            result = subprocess.run(['/usr/bin/otool', '-L', str(binary)], capture_output=True,
                                    text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise contained.ContainmentUnavailable('cannot inspect native shell libraries') from error
        for line in result.stdout.splitlines()[1:]:
            name = line.strip().split(' (compatibility version ', 1)[0]
            if not name.startswith('/'):
                raise contained.ContainmentUnavailable('native shell requires absolute library paths')
            name = os.path.normpath(name)
            if name.startswith(('/System/', '/usr/lib/')):
                continue  # Already readable in the standard Seatbelt system-library policy.
            try:
                library = Path(name).resolve(strict=True)
            except OSError as error:
                raise contained.ContainmentUnavailable('native shell library is missing') from error
            if not library.is_file():
                raise contained.ContainmentUnavailable('native shell library is not a file')
            _checked_root(Path(name).parent)
            _checked_root(library.parent)
            files.update((Path(name), library))
            pending.append(library)
    return tuple(sorted(files))


def source() -> Path:
    """arm64e platform Bash is not a portable third-party arm64 executable."""
    architecture = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
    candidates = [shutil.which('bash'), '/opt/homebrew/bin/bash', '/usr/local/bin/bash', '/bin/bash']
    if architecture == 'x86_64':
        candidates.insert(0, '/bin/bash')
    for name in dict.fromkeys(candidates):
        if not name or not os.access(name, os.X_OK):
            continue
        try:
            binary = Path(name).resolve(strict=True)
            result = subprocess.run(['/usr/bin/lipo', '-archs', str(binary)], capture_output=True,
                                    text=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        if architecture not in result.stdout.split():
            continue
        _checked_root(binary.parent)
        read_files(binary)
        return binary
    raise contained.ContainmentUnavailable(
        f'native macOS requires a {architecture} Bash (not arm64e); install with brew install bash')
