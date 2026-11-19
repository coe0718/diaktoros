"""The ``selftest`` command marks its loop as loaded from its file (#2525, #2561 review F3).

``cmd_selftest`` wraps ``config.load_id``'s answer in ``onboarding_record.from_file``; that
marker is what makes selftest read the loop file again after its checks. Without it the
command's loop is treated as built in memory, so an edit made on disk during the run, or a
file removed before the first snapshot, would let the run pass. These tests drive the real
command with the real ``config.load_id`` over a real loop file.

GitHub, the model and bubblewrap are mocked by ``test_selftest.SelftestBase``; the seats
resolve from their profile through a stub resolver.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_selftest import MODEL_KEY, SelftestBase  # noqa: E402
from diaktoros import onboarding_record, seat_model, selftest  # noqa: E402
from diaktoros.cli import checks  # noqa: E402


class SelftestCommandLoopFile(SelftestBase):
    def setUp(self):
        super().setUp()
        cfg = self.fx.root / "cfg"
        cfg.mkdir()
        self.file = cfg / f"{self.fx.loop['id']}.json"
        self.raw = json.loads(json.dumps(self.fx.loop))
        for seat in ("reviewer", "fixer"):
            self.raw["seats"][seat].update(route=seat, profile="default")
        self.file.write_text(json.dumps(self.raw))
        for patch in (mock.patch.dict(os.environ, {"DIAKTOROS_CONFIG_DIR": str(cfg)}),
                      mock.patch.object(seat_model, "resolve_profile", side_effect=self.seat)):
            patch.start()
            self.addCleanup(patch.stop)

    def seat(self, profile, seat, settings):
        return seat_model.SeatInference(seat, profile, "profile", "prov", "tiny-model",
                                        self.fx.settings["upstream"], MODEL_KEY)

    def command(self):
        args = argparse.Namespace(loop=self.fx.loop["id"], pr=None, no_model=True,
                                  live_turn=False, timeout=None, ping=False, admin_token=None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = checks.cmd_selftest(args)
        return rc, out.getvalue()

    def edit_mid_run(self, edit):
        real = selftest.check_identities

        def check_then_edit(report, loop):
            real(report, loop)
            edit()
        with mock.patch.object(selftest, "check_identities", side_effect=check_then_edit):
            return self.command()

    def test_an_untouched_loop_file_passes_through_the_command(self):
        rc, text = self.edit_mid_run(lambda: None)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("inputs:changed", text)

    def test_a_setting_edited_on_disk_mid_run_fails_the_command(self):
        rc, text = self.edit_mid_run(
            lambda: self.file.write_text(json.dumps({**self.raw, "base": "develop"})))
        self.assertEqual(rc, 1, text)
        self.assertIn("changed while the checks ran: loop", text)
        self.assertNotIn("the isolated path is ready", text)

    def test_a_loop_file_removed_before_the_first_snapshot_fails_the_command(self):
        real, calls = onboarding_record.inputs_before, []

        def unlink_then_snapshot(loop, **kwargs):
            if not calls:
                self.file.unlink()
            calls.append(loop)
            return real(loop, **kwargs)
        with mock.patch.object(onboarding_record, "inputs_before",
                               side_effect=unlink_then_snapshot):
            rc, text = self.command()
        self.assertEqual(rc, 1, text)
        self.assertIn("changed while the checks ran: identities, loop, model, loop file", text)
        self.assertNotIn("the isolated path is ready", text)


if __name__ == "__main__":
    unittest.main()
