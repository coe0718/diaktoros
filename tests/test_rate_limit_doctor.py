"""#849: doctor warns when the gateway's ``rate_limit`` is below a burst the loop should absorb.

A module of its own that imports nothing the change added, so on a tree without the check these
tests import and FAIL (AttributeError on ``check_gateway_rate_limit`` / ``gateway_rate_limit``)
rather than being missing. The roster of ``check_loop`` is held by ``tests/harness/doctor.py``
(``python3 tests/run_tests.py doctor``)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
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


if __name__ == "__main__":
    unittest.main()
