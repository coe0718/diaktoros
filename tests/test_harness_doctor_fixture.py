"""#690: the harness's own fixtures write inside its temp root, never into the operator's home.

``doctor_runtime_fixture`` builds a host Node for ``runtime:node``, because the check under test
resolves the host through ``HERMES_REAL_HOME`` — which a Hermes-run shell exports naming the
operator's live home. Written at ``$HERMES_REAL_HOME/.hermes/node/bin/node``, that stub replaced
the real Node (147,870,928 bytes, "Node 26.7.0") with 23 bytes of ``echo v22.0.0``, and every
JavaScript suite after it exited 0 while running nothing.

So: the fixture builds its host under ``TMP``, the group pins the variable at a home inside it, and
the guard refuses (loudly) a path that would escape. These tests are the ones that fail if that
regresses — the harness's own suite ran green (2003/2003) while overwriting the live binary.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from harness import doctor as harness_doctor  # noqa: E402

from diaktoros import runtime_detect  # noqa: E402


class InstallTheFixture(unittest.TestCase):
    """The fixture's paths are the caller's to remove, so removing them is the test's job too.

    Not optional bookkeeping: the runtime file lives in the shared temp Hermes home, and a later
    reader in the same discovery run depends on its absence — a gate deciding whether an isolated
    worker is available reads it. A test that installs it and walks away turns
    ``test_review_command``'s "queued, waiting — isolated worker unavailable" into the success path,
    and the failure then surfaces in *that* test, two files later.
    """

    def install(self) -> list[pathlib.Path]:
        (harness_doctor.TMP / "hermes-home").mkdir(parents=True, exist_ok=True)
        added = harness_doctor.doctor_runtime_fixture()
        for path in added:
            self.addCleanup(path.unlink, missing_ok=True)
        return added


class AFixtureCannotWriteOutsideTheTempRoot(InstallTheFixture):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        # A decoy "operator home" in the shape a Hermes-run shell exports. Nothing may appear here.
        self.decoy = pathlib.Path(temp.name) / "operator-home"
        self.decoy.mkdir()
        (self.decoy / ".hermes" / "node" / "bin").mkdir(parents=True)
        self.real_node = self.decoy / ".hermes" / "node" / "bin" / "node"
        self.real_node.write_bytes(b"the operator's live Node, which must not move")

    def digests(self):
        return {path.name: harness_doctor.binary_digest(path)
                for path in sorted(self.decoy.rglob("*")) if path.is_file()}

    def test_the_fixture_builds_its_host_inside_the_temp_root(self):
        with mock.patch.dict(os.environ, {"HERMES_REAL_HOME": str(self.decoy)}):
            added = self.install()
        node = harness_doctor.OPERATOR_HOME / ".hermes" / "node" / "bin" / "node"
        self.assertTrue(node.is_file())
        self.assertIn(node, added, "the fixture's host Node is a path the caller removes")
        for path in added:
            self.assertEqual(harness_doctor._outside_tmp(path), "", f"{path} escapes the temp root")

    def test_the_named_operator_home_is_untouched(self):
        before = self.digests()
        with mock.patch.dict(os.environ, {"HERMES_REAL_HOME": str(self.decoy)}):
            self.install()
        self.assertEqual(self.digests(), before)
        self.assertEqual(self.real_node.read_bytes(),
                         b"the operator's live Node, which must not move")

    def test_everything_it_creates_is_a_path_the_caller_removes(self):
        """The contract cleanup rests on: what the fixture writes, it hands back."""
        with harness_doctor.fake_operator_home():
            added = self.install()
        created = {path.name for path in added if path.exists()}
        self.assertIn("diaktoros-runtime.json", created)
        self.assertIn("node", created)
        with harness_doctor.fake_operator_home():
            for path in added:
                path.unlink(missing_ok=True)
            self.assertFalse((harness_doctor.TMP / "hermes-home"
                              / "diaktoros-runtime.json").exists())

    def test_the_write_guard_refuses_an_escaping_path(self):
        with self.assertRaises(AssertionError):
            harness_doctor.inside_tmp(pathlib.Path("/etc/passwd"))
        self.assertTrue(str(harness_doctor.fixture_path("x")).startswith(str(harness_doctor.TMP)))


class ThePinIsWhatMakesThatHostTheOneUnderTest(InstallTheFixture):
    def test_pinned_resolution_finds_the_fixture_node(self):
        with harness_doctor.fake_operator_home():
            self.install()
            root, version = runtime_detect.node_version()
        self.assertEqual((str(root) if root else None, version),
                         (str(harness_doctor.OPERATOR_HOME / ".hermes" / "node"), "v22.0.0"))

    def test_the_pin_restores_the_variable(self):
        before = os.environ.get("HERMES_REAL_HOME")
        with harness_doctor.fake_operator_home():
            self.assertEqual(os.environ["HERMES_REAL_HOME"], str(harness_doctor.OPERATOR_HOME))
        self.assertEqual(os.environ.get("HERMES_REAL_HOME"), before)

    def test_the_pin_unsets_a_variable_it_found_unset(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HERMES_REAL_HOME", None)
            with harness_doctor.fake_operator_home():
                self.assertIn("HERMES_REAL_HOME", os.environ)
            self.assertNotIn("HERMES_REAL_HOME", os.environ)

    def test_the_group_runs_under_the_pin(self):
        """The registered group is the pinned one, not the raw function."""
        self.assertIn("doctor", harness_doctor.GROUPS)
        self.assertIsNot(harness_doctor.GROUPS["doctor"], harness_doctor.group_doctor)
        self.assertEqual(harness_doctor.GROUPS["doctor"].__name__, "group_doctor")


if __name__ == "__main__":
    unittest.main()
