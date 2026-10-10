"""Experimental Hermes-only adaptation to the host-owned native process group.

Kernel syscall denial supplies containment. This changes the trusted Hermes
runtime's subprocess behavior, not arbitrary executed tools. Hermes already
recognizes a shared process group and avoids signalling its own group.
"""
import subprocess


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
    subprocess._USE_VFORK = False
    subprocess.Popen = OwnedPopen
