"""#2244: the same ``set`` run again after its observer route write failed finishes the change.

A route write after the adjudication change is committed can still fail (#2212): ``set`` exits 2,
says the adjudication change stays written and leaves the loop config otherwise unchanged. The
operator is told to check ``status`` and run ``set`` again; that rerun must find the adjudication
change already made ("already on"/"already off"), write the observer route and exit 0.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, routes  # noqa: E402
import test_gate_shims as tgs  # noqa: E402


class RerunAfterRouteFailure(tgs.Base):
    def rerun(self, *adjudication):
        """Run ``set`` with the observer route write failing, then the same ``set`` again."""
        argv = ["set", "--loop", "widgets", *adjudication, "--observer-profile", "default",
                "--observer-route", "widgets-watch"]
        real = routes.new_route

        def new_route(name, **kwargs):
            if name == "widgets-watch":
                raise OSError("disk full")
            return real(name, **kwargs)
        with patch.object(routes, "new_route", side_effect=new_route):
            rc, out = self.run_cli(argv)
        self.assertEqual(rc, 2, out)
        self.assertIn("but the adjudication change above stays written", out)
        self.assertNotIn("widgets-watch", self.loop_routes())
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.loop_routes()["widgets-watch"]["script"], "observe.py")
        self.assertEqual(config.load_id("widgets")["observer"]["route"], "widgets-watch")
        return out

    def test_rerun_after_turning_adjudication_on(self):
        self.install("acme/widgets")
        out = self.rerun("--adjudicator-profile", "arbiter")
        self.assertIn("adjudication already on (route widgets-breach, profile arbiter)", out)
        self.assertEqual(config.load_id("widgets")["adjudicator"]["profile"], "arbiter")
        self.assertIn("widgets-breach", self.loop_routes())

    def test_rerun_after_turning_adjudication_off(self):
        self.install("acme/widgets", "--adjudicator-route", "widgets-breach",
                     "--adjudicator-profile", "arbiter")
        out = self.rerun("--adjudicator", "off")
        self.assertIn("adjudication already off", out)
        self.assertEqual(config.load_id("widgets")["adjudicator"], {})
        self.assertNotIn("widgets-breach", self.loop_routes())


if __name__ == "__main__":
    unittest.main()
