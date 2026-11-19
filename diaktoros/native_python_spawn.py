"""Experimental Hermes-only adaptation to the host-owned native process group.

Kernel syscall denial supplies containment. This changes the trusted Hermes
runtime's subprocess behavior, not arbitrary executed tools. Hermes already
recognizes a shared process group and avoids signalling its own group.
"""
import subprocess


def entry(argv: list[str]) -> list[str]:
    # -P excludes the writable cwd while preserving trusted PYTHONPATH roots.
    return [argv[0], '-P', '-c',
            'import runpy,sys; from diaktoros.native_python_spawn import install; '
            'install(); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")',
            *argv[1:]]


def install():
    parent = subprocess.Popen

    class OwnedPopen(parent):
        def __init__(self, *args, **kwargs):
            if kwargs.get('process_group') not in (None, -1):
                raise ValueError('native turn tools must retain the owned process group')
            # The runtime's terminal backend normally asks for an independent
            # session. Its existing shared-group cleanup branch handles this
            # adaptation; the independent watchdog owns the whole turn group.
            kwargs['start_new_session'] = False
            super().__init__(*args, **kwargs)

    subprocess._USE_POSIX_SPAWN = False
    if hasattr(subprocess, '_USE_VFORK'):
        subprocess._USE_VFORK = False
    subprocess.Popen = OwnedPopen
