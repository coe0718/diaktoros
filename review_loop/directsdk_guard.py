"""Fail-closed native launch contract, owned by review-loop, not the provider."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

# Inventory-only server reviewed at provider ef73726. Changed code needs review.
INERT_SHA256 = 'bf20a09b2358600981b115c32845ec24722cb66adfc18d247d1c35ed999a0e0a'


class NativeLaunchRefused(ValueError):
    """Safe diagnostic; never include argv, paths or file contents."""


def _private_file(path, root, name):
    path = Path(path)
    if path != root / name or path.is_symlink() or not path.is_file():
        raise NativeLaunchRefused('native launch lockdown refused')
    if path.stat().st_size > 8 * 1024 * 1024:
        raise NativeLaunchRefused('native launch lockdown refused')
    return path


def validate_launch(argv, kwargs, command, plugin):
    """Accept only the reviewed one-turn transport grammar, including its MCP code."""
    def refuse():
        raise NativeLaunchRefused('native launch lockdown refused')
    if not isinstance(argv, (list, tuple)) or not argv or argv[0] != command:
        refuse()
    values = {}
    switches = {'-p', '--verbose', '--include-partial-messages', '--strict-mcp-config',
                '--disable-slash-commands', '--no-session-persistence'}
    pairs = {'--model', '--input-format', '--output-format', '--tools', '--system-prompt-file',
             '--settings', '--setting-sources', '--max-turns', '--permission-mode', '--mcp-config', '--effort'}
    i = 1
    while i < len(argv):
        key = argv[i]
        if key in values or key not in switches | pairs:
            refuse()
        if key in switches:
            values[key] = True
            i += 1
        else:
            if i + 1 >= len(argv) or not isinstance(argv[i + 1], str):
                refuse()
            values[key] = argv[i + 1]
            i += 2
    if not switches <= values.keys():
        refuse()
    required = {'--tools': '', '--setting-sources': '', '--max-turns': '1',
                '--permission-mode': 'dontAsk', '--input-format': 'stream-json', '--output-format': 'stream-json'}
    if any(values.get(k) != v for k, v in required.items()):
        refuse()
    if not {'--model', '--settings', '--system-prompt-file', '--mcp-config'} <= values.keys():
        refuse()
    try:
        root = Path(values['--settings']).parent
        mode = root.stat()
        if root.is_symlink() or not root.is_absolute() or mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
            refuse()
        settings = _private_file(values['--settings'], root, 'settings.json')
        _private_file(values['--system-prompt-file'], root, 'system.md')
        manifest = _private_file(str(root / 'tools.json'), root, 'tools.json')
        data = json.loads(settings.read_text())
        if set(data) != {'env'} or set(data['env']) != {'CLAUDE_CODE_EXTRA_BODY'}:
            refuse()
        inventory = Path(plugin) / 'inert_mcp.py'
        if inventory.is_symlink() or hashlib.sha256(inventory.read_bytes()).hexdigest() != INERT_SHA256:
            refuse()
        expected = {'mcpServers': {'hermes': {'command': sys.executable, 'args': [str(inventory), str(manifest)]}}}
        if json.loads(values['--mcp-config']) != expected:
            refuse()
        cwd = Path(kwargs['cwd'])
        mode = cwd.stat()
        if not cwd.is_absolute() or cwd.is_symlink() or mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
            refuse()
        if kwargs.get('shell') or kwargs.get('executable'):
            refuse()
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        refuse()


def install_guard(command, plugin):
    """Install before provider import/Client construction; no unguarded launch allowed."""
    original = subprocess.Popen
    def guarded(argv, *args, **kwargs):
        if args:
            raise NativeLaunchRefused('native launch lockdown refused')
        validate_launch(argv, kwargs, command, plugin)
        return original(argv, **kwargs)
    subprocess.Popen = guarded
    # The validated Popen must not route through os.posix_spawn (refused below); use fork_exec.
    subprocess._USE_POSIX_SPAWN = False
    for name in REFUSED_OS_LAUNCHERS:
        if hasattr(os, name):
            setattr(os, name, _refuse_launch)
    return original


# Other ways to start or replace a process; the transport only needs the guarded Popen.
REFUSED_OS_LAUNCHERS = (
    'posix_spawn', 'posix_spawnp', 'fork', 'forkpty', 'system',
    'execl', 'execle', 'execlp', 'execlpe', 'execv', 'execve', 'execvp', 'execvpe',
    'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe',
)


def _refuse_launch(*args, **kwargs):
    raise NativeLaunchRefused('native launch lockdown refused')
