"""#2595: doctor's ``plugin-version`` check and ``hermes dk update`` name the same installed commit
the claimant does: the HEAD of the install folder under ``$HERMES_HOME/plugins``, not the HEAD of
the checkout this process loaded.

Before the fix doctor read the loaded checkout, so a process that loaded another copy (the stale
claimant #2572 catches) had doctor and ``update`` call that copy's commit the installed one.
Real git checkouts in temp folders, as in ``test_claimant_install``; GitHub is stubbed.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import contextlib
import inspect
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_claimant_install import _checkout  # noqa: E402
from diaktoros import claimant, config, doctor, gh  # noqa: E402
from diaktoros.cli import update as cli_update  # noqa: E402

TAG = "v9.9.9"


def _doctor_installed_commit():
    """``doctor.installed_commit`` as doctor's source defines it, bound to doctor's globals.
    ``tests/harness/doctor.py`` pins it to a constant for the rest of the process once imported,
    so in a shared run the module attribute is not the real function: rebuild it."""
    tree = ast.parse(inspect.getsource(doctor))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "installed_commit")
    found: dict = {}
    exec(compile(ast.Module([node], []), doctor.__file__, "exec"), vars(doctor), found)
    return found["installed_commit"]


@unittest.skipUnless(shutil.which("git"), "needs a git binary to make the checkouts")
class InstalledCommitIsTheInstallFolders(unittest.TestCase):
    def setUp(self):
        self.plugins = config.home() / "plugins"
        self.addCleanup(shutil.rmtree, self.plugins, ignore_errors=True)
        loaded = pathlib.Path(tempfile.mkdtemp()) / "loaded"
        self.addCleanup(shutil.rmtree, loaded.parent, ignore_errors=True)
        self.loaded = _checkout(loaded)
        # This process "loaded" the temp checkout: both modules' idea of their own checkout.
        for target, name, value in ((claimant, "plugin_root", lambda: loaded),
                                    (claimant, "LOADED", self.loaded),
                                    (doctor, "plugin_root", lambda: loaded),
                                    (doctor, "installed_commit", _doctor_installed_commit())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.paths: list[str] = []

    def fake(self, loop, path, method="GET", body=None, login=None):
        self.paths.append(path)
        if path.endswith("/releases/latest"):
            return gh.Response({"tag_name": TAG}, "", 200, {})
        if "/compare/" in path:
            return gh.Response({"status": "ahead", "ahead_by": 0, "commits": []}, "", 200, {})
        return gh.Response(None, "HTTP 404", 404, {})

    def install(self) -> str:
        installed = _checkout(self.plugins / "diaktoros")
        self.assertNotEqual(installed, self.loaded)
        return installed

    def test_doctor_and_the_claimant_name_the_same_installed_commit(self):
        installed = self.install()
        self.assertEqual(claimant.installed_commit(), installed)
        self.assertEqual(doctor.installed_commit(), installed)
        self.assertIn(f"loaded plugin {self.loaded[:7]}, but {installed[:7]} is installed",
                      claimant.stale())

    def test_plugin_version_compares_the_installed_commit_not_the_loaded_one(self):
        installed = self.install()
        with mock.patch.object(gh, "request", side_effect=self.fake):
            check = doctor.check_plugin_version({"id": "w"})
        self.assertIn(f"installed {installed[:7]}", check.detail)
        compares = [p for p in self.paths if "/compare/" in p]
        self.assertTrue(compares)
        self.assertTrue(all(installed in p for p in compares), compares)
        self.assertFalse(any(self.loaded in p for p in self.paths), self.paths)

    def test_update_reports_the_installed_commit_not_the_loaded_one(self):
        installed = self.install()
        out = io.StringIO()
        with mock.patch.object(gh, "request", side_effect=self.fake), \
                contextlib.redirect_stdout(out):
            got, _, _ = cli_update._report({"id": "w"}, "main")
        self.assertEqual(got, installed)
        self.assertIn(f"installed: {installed[:7]}", out.getvalue())
        self.assertNotIn(self.loaded[:7], out.getvalue())
        self.assertTrue(any(p.endswith(f"/compare/{installed}...main") for p in self.paths),
                        self.paths)

    def test_without_an_install_folder_the_loaded_checkout_is_the_install(self):
        # A development run: no folder under $HERMES_HOME/plugins, so both read the checkout.
        self.assertEqual(doctor.installed_commit(), self.loaded)
        self.assertEqual(claimant.installed_commit(), self.loaded)


if __name__ == "__main__":
    unittest.main()
