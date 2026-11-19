"""Follow-ups to #2512's onboarding fingerprints: stale reasons and selftest's inputs:changed.

* #2514: a stale stage's reason names what its fingerprint now covers (credential contents for
  ``identities``, loop settings for ``loop``).
* #2525: selftest's mid-run ``inputs:changed`` check reads the loop file again, so a setting
  edited on disk while the checks run is seen.
* #2526: a stage that ``onboarding.record`` stores ``failed`` because its inputs changed after
  that check also fails the run.

GitHub, the model and bubblewrap are mocked by ``test_selftest.SelftestBase``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_selftest import SelftestBase  # noqa: E402
from diaktoros import config, onboarding, selftest  # noqa: E402


def rotate(path: pathlib.Path, letter: str) -> None:
    path.write_text("ghp_" + letter * 36 + "\n")


class StaleReasons(SelftestBase):
    """#2514: the reason says which kind of input changed, including the new ones."""

    def record_clean_run(self):
        rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 0, text)

    def test_a_rotated_token_names_the_credential_file_contents(self):
        self.record_clean_run()
        rotate(pathlib.Path(self.fx.loop["tokens"]["reviewer"]), "Z")
        row = onboarding.inspect(self.fx.loop, runtime_file=self.fx.runtime_file)["identities"]
        self.assertEqual(row["status"], onboarding.STALE, row)
        self.assertIn("identities fingerprint changed", row["reason"])
        self.assertIn("the contents of a token or App key file", row["reason"])

    def test_a_changed_loop_setting_names_a_loop_setting(self):
        self.record_clean_run()
        edited = {**self.fx.loop, "host": "hooks.example.net"}
        rows = onboarding.inspect(edited, runtime_file=self.fx.runtime_file)
        self.assertEqual(rows["identities"]["status"], onboarding.PASSED, rows)
        self.assertEqual(rows["loop"]["status"], onboarding.STALE, rows)
        self.assertIn("loop fingerprint changed", rows["loop"]["reason"])
        self.assertIn("a loop setting such as host or base", rows["loop"]["reason"])


class LoopFileEditedDuringSelftest(SelftestBase):
    """#2525: the loop as its file holds it after the checks, not the copy they ran with.

    The fixture's loop is not a complete config, so ``config.load_id`` is replaced by a plain
    read of the file; the file itself is real and is what the edits change."""

    def setUp(self):
        super().setUp()
        self.cfg = self.fx.root / "cfg"
        self.cfg.mkdir()
        self.file = self.cfg / f"{self.fx.loop['id']}.json"
        self.file.write_text(json.dumps(self.fx.loop))
        for patch in (mock.patch.dict(os.environ, {"DIAKTOROS_CONFIG_DIR": str(self.cfg)}),
                      mock.patch.object(config, "load_id", side_effect=self.load)):
            patch.start()
            self.addCleanup(patch.stop)

    def load(self, loop_id):
        return json.loads((self.cfg / f"{loop_id}.json").read_text())

    def run_editing(self, edit):
        real = selftest.check_identities

        def check_then_edit(report, loop):
            real(report, loop)
            edit()
        with mock.patch.object(selftest, "check_identities", side_effect=check_then_edit):
            return self.run_selftest(model=False)

    def test_an_untouched_loop_file_still_passes(self):
        rc, text = self.run_editing(lambda: None)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("inputs:changed", text)

    def test_a_setting_edited_on_disk_mid_run_fails_inputs_changed(self):
        def edit_base():
            self.file.write_text(json.dumps({**self.fx.loop, "base": "develop"}))
        rc, text = self.run_editing(edit_base)
        self.assertEqual(rc, 1, text)
        self.assertIn("changed while the checks ran: loop ", text)
        self.assertNotIn("the isolated path is ready", text)

    def test_a_loop_file_that_cannot_be_loaded_mid_run_fails_every_stage(self):
        rc, text = self.run_editing(lambda: self.file.write_text("{not json"))
        self.assertEqual(rc, 1, text)
        self.assertIn("changed while the checks ran: identities, loop, model", text)

    def test_a_loop_file_removed_mid_run_fails_inputs_changed(self):
        rc, text = self.run_editing(self.file.unlink)
        self.assertEqual(rc, 1, text)
        self.assertIn("changed while the checks ran: loop file", text)


class InputsChangedWhileRecording(SelftestBase):
    """#2526: the window between the last ``inputs:changed`` check and the record."""

    def stored(self):
        return onboarding._load(self.fx.loop)

    def test_a_token_rotated_inside_the_recorder_fails_the_run(self):
        token = pathlib.Path(self.fx.loop["tokens"]["reviewer"])
        real = onboarding.record

        def rotate_then_record(loop, stage, *args, **kwargs):
            if stage == "identities":
                rotate(token, "Q")
            return real(loop, stage, *args, **kwargs)
        with mock.patch.object(onboarding, "record", side_effect=rotate_then_record):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1, text)
        self.assertIn("inputs:changed", text)
        self.assertIn("changed before the result was recorded: identities", text)
        self.assertNotIn("the isolated path is ready", text)
        stored = self.stored()
        self.assertEqual(stored["identities"]["status"], onboarding.FAILED, stored)
        self.assertEqual(stored["identities"]["fingerprint"], "")

    def test_a_change_seen_mid_run_is_reported_once(self):
        token = pathlib.Path(self.fx.loop["tokens"]["reviewer"])
        real = selftest.check_identities

        def check_then_rotate(report, loop):
            real(report, loop)
            rotate(token, "W")
        report = []
        real_add = selftest.Report.add

        def add(this, step, name, status, *rest):
            report.append((name, status))
            return real_add(this, step, name, status, *rest)
        with mock.patch.object(selftest, "check_identities", side_effect=check_then_rotate), \
                mock.patch.object(selftest.Report, "add", add):
            rc, text = self.run_selftest(model=False)
        self.assertEqual(rc, 1, text)
        self.assertEqual(report.count(("inputs:changed", selftest.FAIL)), 1, text)


if __name__ == "__main__":
    unittest.main()
