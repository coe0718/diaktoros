"""Measure kernel syscall filtering and spawn-attribute escapes; no production policy.

All probe children are immediately waited for. No detached fixture survives.
"""
import _home_guard  # noqa: F401
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from diaktoros import seatbelt


PROBE = r'''
#define _DARWIN_C_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <spawn.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;

int main(int argc, char **argv) {
    if (argc != 2) return 20;
    puts("PROBE_STARTED"); fflush(stdout);
    pid_t child = fork();
    if (child < 0) return 21;
    if (child == 0) {
        int rc = 0;
        if (!strcmp(argv[1], "setsid")) rc = setsid() < 0 ? errno : 0;
        else if (!strcmp(argv[1], "setpgid")) rc = setpgid(0, 0) < 0 ? errno : 0;
        else if (!strcmp(argv[1], "raw_setsid")) rc = syscall(SYS_setsid) < 0 ? errno : 0;
        else if (!strcmp(argv[1], "raw_setpgid")) rc = syscall(SYS_setpgid, 0, 0) < 0 ? errno : 0;
        else if (!strcmp(argv[1], "fork_exec")) {
            execl("/usr/bin/true", "true", NULL);
            _exit(22);
        } else {
            posix_spawnattr_t attrs;
            if (posix_spawnattr_init(&attrs)) _exit(23);
            short flags = 0;
            if (!strcmp(argv[1], "spawn_session")) flags = POSIX_SPAWN_SETSID;
            else if (!strcmp(argv[1], "spawn_group")) flags = POSIX_SPAWN_SETPGROUP;
            else if (strcmp(argv[1], "spawn_plain")) _exit(24);
            if (posix_spawnattr_setflags(&attrs, flags) ||
                posix_spawnattr_setpgroup(&attrs, 0)) _exit(25);
            pid_t spawned;
            char *args[] = {"true", NULL};
            rc = posix_spawn(&spawned, "/usr/bin/true", NULL, &attrs, args, environ);
            posix_spawnattr_destroy(&attrs);
            if (!rc) {
                int status;
                if (waitpid(spawned, &status, 0) != spawned ||
                    !WIFEXITED(status) || WEXITSTATUS(status)) _exit(26);
            }
        }
        if (rc && rc != EPERM && rc != EACCES) _exit(27);
        _exit(rc ? 10 : 0);
    }
    int status;
    if (waitpid(child, &status, 0) != child) return 28;
    if (WIFSIGNALED(status)) printf("BLOCKED_SIGNAL=%d\n", WTERMSIG(status));
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 10) puts("BLOCKED_ERRNO");
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 0) puts("ALLOWED");
    else return 29;
    return 0;
}
'''


class NativeDetachRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = seatbelt.unavailable()
        if reason:
            if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_LIFECYCLE') == '1':
                raise RuntimeError(reason)
            raise unittest.SkipTest(reason)
        cls.directory = tempfile.TemporaryDirectory(prefix='dk-detach-', dir='/tmp')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name).resolve()
        source = cls.root / 'probe.c'
        source.write_text(PROBE)
        cls.program = cls.root / 'probe'
        compiled = subprocess.run(['/usr/bin/clang', '-Werror', '-Wno-deprecated-declarations',
                                   str(source), '-o', str(cls.program)],
                                  capture_output=True, text=True, timeout=30)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def matrix(self, *, blocked_syscalls, blocked_modes):
        profile = seatbelt.profile(read_roots=(self.root,), write_roots=())
        if blocked_syscalls:
            # Numeric IDs come from XNU syscalls.master: setpgid=82, setsid=147,
            # posix_spawn=244. This candidate stays entirely in this probe.
            profile = replace(profile, text=profile.text + '(allow syscall-unix)\n'
                              '(deny syscall-unix (syscall-number '
                              + ' '.join(map(str, blocked_syscalls)) + '))\n')
        policy = self.root / 'policy.sb'
        policy.write_text(profile.text)
        for mode in ('setsid', 'setpgid', 'raw_setsid', 'raw_setpgid',
                     'spawn_session', 'spawn_group', 'spawn_plain', 'fork_exec'):
            with self.subTest(mode=mode):
                result = subprocess.run(profile.command(policy, [str(self.program), mode]),
                                        env={'PATH': '/usr/bin:/bin'}, cwd=self.root,
                                        capture_output=True, text=True, timeout=10)
                detail = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, detail)
                self.assertIn('PROBE_STARTED', result.stdout)
                denied = 'BLOCKED_' in result.stdout
                print(f'DETACH_ROUTE syscalls={blocked_syscalls} mode={mode} '
                      f'blocked={denied}', flush=True)
                self.assertEqual(denied, mode in blocked_modes, detail)

    def test_existing_profile_permits_detach_routes(self):
        self.matrix(blocked_syscalls=(), blocked_modes=())

    def test_direct_syscall_denial_leaves_spawn_attribute_escape(self):
        self.matrix(blocked_syscalls=(82, 147),
                    blocked_modes=('setsid', 'setpgid', 'raw_setsid', 'raw_setpgid'))

    def test_spawn_denial_also_blocks_ordinary_spawn(self):
        self.matrix(blocked_syscalls=(82, 147, 244), blocked_modes=(
            'setsid', 'setpgid', 'raw_setsid', 'raw_setpgid',
            'spawn_session', 'spawn_group', 'spawn_plain'))


if __name__ == '__main__':
    unittest.main()
