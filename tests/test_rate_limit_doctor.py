"""#849: doctor warns when the gateway's ``rate_limit`` is below a burst the loop should absorb.

A module of its own that imports nothing the change added, so on a tree without the check these
tests import and FAIL (AttributeError on ``check_gateway_rate_limit`` / ``gateway_rate_limit``)
rather than being missing. The roster of ``check_loop`` is held by ``tests/harness/doctor.py``
(``python3 tests/run_tests.py doctor``)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import doctor, gate_failures  # noqa: E402


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
            [sys.executable, "-c", "import fcntl,sys;f=open(sys.argv[1]);"
             "fcntl.flock(f,fcntl.LOCK_EX);print('ok',flush=True);sys.stdin.read()", str(lock)],
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


if __name__ == "__main__":
    unittest.main()
