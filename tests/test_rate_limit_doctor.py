"""#849: doctor warns when the gateway's ``rate_limit`` is below a burst the loop should absorb.

A module of its own that imports nothing the change added, so on a tree without the check these
tests import and FAIL (AttributeError on ``check_gateway_rate_limit`` / ``gateway_rate_limit``)
rather than being missing. The roster of ``check_loop`` is held by ``tests/harness/doctor.py``
(``python3 tests/run_tests.py doctor``)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import doctor, gate_failures, util  # noqa: E402


class RateLimitDoctor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        env = mock.patch.dict("os.environ", {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = {"id": "w", "repo": "o/w", "seats": {}}
        patch = mock.patch.object(doctor, "gate_timeout_profiles", return_value={"default": ["reviewer"]})
        patch.start()
        self.addCleanup(patch.stop)

    def config(self, data):
        (self.home / "config.yaml").write_text(json.dumps(data))   # JSON is valid YAML

    def test_the_default_limit_of_30_is_a_warning_with_the_line_to_set(self):
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.name, "gate:rate_limit")
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("30/min", check.detail)
        self.assertIn("HTTP 429", check.detail)
        self.assertIn("platforms.webhook.extra.rate_limit: 120", check.fix)
        self.assertIn(str(self.home / "config.yaml"), check.fix)

    def test_a_limit_that_absorbs_a_burst_passes_and_a_low_set_one_warns(self):
        self.config({"platforms": {"webhook": {"extra": {"rate_limit": 120}}}})
        self.assertEqual(doctor.check_gateway_rate_limit(self.loop).status, doctor.VERIFIED)
        self.config({"platforms": {"webhook": {"extra": {"rate_limit": 59}}}})
        self.assertEqual(doctor.check_gateway_rate_limit(self.loop).status, doctor.UNKNOWN)
        self.config({"platforms": {"webhook": {"rate_limit": "lots"}}})
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("not a number", check.detail)

    def test_the_gateway_rate_limit_is_read_with_the_timeout_resolution(self):
        self.config({"platforms": {"webhook": {"rate_limit": 90, "extra": {"rate_limit": 45}}}})
        self.assertEqual(gate_failures.gateway_rate_limit()[0], 45)    # extra wins, as the gateway
        self.assertEqual(gate_failures.gateway_script_timeout()[0], 30)


class ProfileGatewayNotRunning(unittest.TestCase):
    """#1362: a multiplexed profile's own gateway is judged only when it runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        env = mock.patch.dict("os.environ", {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = {"id": "w", "repo": "o/w", "seats": {}}
        patch = mock.patch.object(doctor, "gate_timeout_profiles",
                                  return_value={"vex": ["reviewer"], "drey": ["fixer"]})
        patch.start()
        self.addCleanup(patch.stop)
        (self.home / "config.yaml").write_text(
            json.dumps({"platforms": {"webhook": {"extra": {"rate_limit": 120}}}}))
        for name in ("vex", "drey"):
            (self.home / "profiles" / name).mkdir(parents=True)

    def hold(self, home: pathlib.Path, record) -> None:
        """Hold ``home``'s gateway.lock from another process, as a gateway does, until cleanup."""
        lock = home / "gateway.lock"
        lock.write_text(json.dumps(record) if record is not None else "")
        child = subprocess.Popen(
            [sys.executable, "-c", "import fcntl,sys\n"
             "with open(sys.argv[1]) as f:\n"
             "    fcntl.flock(f,fcntl.LOCK_EX);print('ok',flush=True);sys.stdin.read()", str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "ok")

        def stop():
            child.stdin.close()
            child.wait(10)
            child.stdout.close()
        self.addCleanup(stop)

    def test_host_at_120_and_no_profile_gateway_running_is_verified(self):
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("host gateway multiplexing profile vex: 120/min", check.detail)
        self.assertIn("profile vex's own gateway: not running", check.detail)
        self.assertIn("profile drey's own gateway: not running", check.detail)

    def test_profile_locks_held_by_the_host_gateway_are_not_their_own(self):
        self.hold(self.home, {"pid": 4242, "hermes_home": str(self.home)})
        for name in ("vex", "drey"):
            self.hold(self.home / "profiles" / name, {"pid": 4242})
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.VERIFIED, check.detail)
        self.assertIn("held by the host gateway (pid 4242)", check.detail)

    def test_a_running_profile_gateway_below_60_still_warns_with_the_fix_line(self):
        vex = self.home / "profiles" / "vex"
        self.hold(self.home, {"pid": 4242})
        self.hold(vex, {"pid": 5151})
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("profile vex's own gateway (running): 30/min", check.detail)
        self.assertIn(f"set platforms.webhook.extra.rate_limit: 120 in {vex / 'config.yaml'} for "
                      f"the profile vex's own gateway (running)", check.fix)
        self.assertNotIn("drey/config.yaml", check.fix)

    def test_when_it_cannot_tell_it_says_so_and_still_judges_that_gateway(self):
        self.hold(self.home / "profiles" / "vex", None)     # held, but no PID to compare
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("profile vex's own gateway (cannot tell whether it runs:", check.detail)
        self.assertIn("names no PID", check.detail)
        self.assertNotIn("drey", check.fix)

    def test_a_stale_unheld_host_lock_naming_the_profile_gateways_pid_is_not_proof(self):
        """#1988 F1: the host gateway stopped and left its record; the PID in it now belongs to
        the profile's own gateway. That gateway runs at 30/min and must still be judged."""
        vex = self.home / "profiles" / "vex"
        (self.home / "gateway.lock").write_text(json.dumps({"pid": 5151}))   # nobody holds it
        self.hold(vex, {"pid": 5151})
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("profile vex's own gateway (running): 30/min", check.detail)
        self.assertIn(f"in {vex / 'config.yaml'} for the profile vex's own gateway (running)",
                      check.fix)

    def failing(self, real, name: str, exc: OSError):
        """``real`` with ``exc`` raised for every call on a ``gateway.lock`` (path or fd)."""
        def call(target, *args, **kwargs):
            path = (os.readlink(f"/proc/self/fd/{target}") if isinstance(target, int)
                    else os.fspath(target))
            if path.endswith("gateway.lock"):
                raise exc
            return real(target, *args, **kwargs)
        return mock.patch(name, side_effect=call)

    def assert_cannot_tell(self):
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN, check.detail)
        self.assertIn("profile vex's own gateway (cannot tell whether it runs: "
                      "cannot probe the flock on", check.detail)
        self.assertIn(f"in {self.home / 'profiles' / 'vex' / 'config.yaml'}", check.fix)

    def test_a_gateway_lock_that_cannot_be_opened_is_cannot_tell_not_not_running(self):
        """#1988 F2: ``util.flock_held`` gives None on an open error, never False."""
        self.hold(self.home / "profiles" / "vex", {"pid": 5151})
        with self.failing(os.open, "diaktoros.util.os.open", PermissionError(13, "denied")):
            self.assertIsNone(util.flock_held(self.home / "profiles" / "vex" / "gateway.lock"))
            self.assert_cannot_tell()

    def test_a_gateway_lock_whose_flock_fails_is_cannot_tell_not_not_running(self):
        """#1988 F2: ``util.flock_held`` gives None on a ``flock`` error, never False."""
        (self.home / "profiles" / "vex" / "gateway.lock").write_text(json.dumps({"pid": 5151}))
        with self.failing(fcntl.flock, "diaktoros.util.fcntl.flock", OSError(22, "no flock")):
            self.assertIsNone(util.flock_held(self.home / "profiles" / "vex" / "gateway.lock"))
            self.assert_cannot_tell()

    def test_an_unreadable_limit_elsewhere_keeps_the_cannot_tell(self):
        """#1988 F3: the host's limit is unreadable; the profile gateway's own limit reads 120,
        but whether it runs cannot be told, and the result must still say so."""
        vex = self.home / "profiles" / "vex"
        self.config_at(self.home, {"platforms": {"webhook": {"extra": {"rate_limit": "lots"}}}})
        self.config_at(vex, {"platforms": {"webhook": {"extra": {"rate_limit": 120}}}})
        self.hold(vex, None)                                  # held, but no PID to compare
        check = doctor.check_gateway_rate_limit(self.loop)
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertIn("cannot read every gateway's rate_limit", check.detail)
        self.assertIn("profile vex's own gateway (cannot tell whether it runs:", check.detail)
        self.assertIn("names no PID", check.detail)

    @staticmethod
    def config_at(home: pathlib.Path, data) -> None:
        (home / "config.yaml").write_text(json.dumps(data))   # JSON is valid YAML


if __name__ == "__main__":
    unittest.main()
