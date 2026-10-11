"""Bounded host-only APFS churn; capture busy attempts even when retries recover."""
import _home_guard  # noqa: F401
import json
import os
import platform
import time
import unittest

from diaktoros import native_storage, seatbelt


class NativeEjectStress(unittest.TestCase):
    def setUp(self):
        if os.environ.get('DIAKTOROS_REQUIRE_NATIVE_EJECT_STRESS') != '1':
            self.skipTest('explicit real-Mac eject stress opt-in required')
        reason = seatbelt.unavailable()
        if reason:
            self.fail(reason)

    def test_fresh_images_after_idle_metadata_and_fsynced_writes(self):
        busy_events = 0
        for cycle in range(24):
            workload = ('idle', 'metadata', 'fsync')[cycle % 3]
            workspace = native_storage.Workspace(64)
            started = time.monotonic()
            record = {'cycle': cycle, 'workload': workload,
                      'architecture': platform.machine(), 'macos': platform.mac_ver()[0],
                      'turn_processes_launched': False}
            try:
                with workspace:
                    # No child or host cwd is placed on this volume. Every
                    # host file is closed before cleanup to isolate storage.
                    if workload == 'metadata':
                        for index in range(192):
                            path = workspace.work / f'file-{index}'
                            path.write_bytes(b'x' * 4096)
                            path.rename(workspace.work / f'renamed-{index}')
                        for path in workspace.work.iterdir():
                            path.unlink()
                    elif workload == 'fsync':
                        with (workspace.work / 'synced').open('wb') as output:
                            for _ in range(8):
                                output.write(b'x' * (1024 * 1024))
                            output.flush()
                            os.fsync(output.fileno())
                self.assertFalse(workspace.root.exists())
            except BaseException as error:
                record['error'] = str(error)[-2000:]
                raise
            finally:
                record['image'] = str(workspace.root / 'turn.dmg') if workspace.root else None
                record['elapsed_seconds'] = round(time.monotonic() - started, 3)
                record['ejects'] = workspace.cleanup_events
                busy_events += sum('Resource busy' in event.get('error', '')
                                   for event in workspace.cleanup_events)
                # CI logs preserve the first failed attempt, its immediate
                # mount/attachment/holder snapshot, and later recovery together.
                print('NATIVE_EJECT_STRESS ' + json.dumps(record), flush=True)
        print(f'NATIVE_EJECT_STRESS_RESULT cycles=24 busy_events={busy_events} '
              f'architecture={platform.machine()}', flush=True)


if __name__ == '__main__':
    unittest.main()
