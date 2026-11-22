"""#2226: the installed commit is read from the plugin's install folder under
``$HERMES_HOME/plugins``, not from the checkout this process loaded, so a claimant that loaded
another copy (an old plugin folder, a stale checkout a worker was spawned on) is caught.

Real git checkouts in temp folders, both the install and the "loaded" one (``plugin_root`` is
patched to it), so the tests never depend on the runner's own checkout being a git repository
(review on #2572); nothing is read from a real install.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from diaktoros import claimant, config


def _checkout(path: pathlib.Path) -> str:
    """A one-commit git checkout at ``path``; returns its HEAD."""
    path.mkdir(parents=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
           "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "plugin.yaml").write_text(f"name: {path.name}\n")
    subprocess.run([*git, "add", "plugin.yaml"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", f"install {path.name}"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "needs a git binary to make the checkouts")
class InstallFolder(unittest.TestCase):
    def setUp(self):
        self.plugins = config.home() / "plugins"
        self.addCleanup(shutil.rmtree, self.plugins, ignore_errors=True)
        loaded = pathlib.Path(tempfile.mkdtemp()) / "loaded"
        self.addCleanup(shutil.rmtree, loaded.parent, ignore_errors=True)
        head = _checkout(loaded)
        for name, value in (("plugin_root", lambda: loaded), ("LOADED", head)):
            patcher = mock.patch.object(claimant, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_process_loaded_from_another_copy_is_stale(self):
        installed = _checkout(self.plugins / "diaktoros")
        self.assertNotEqual(installed, claimant.LOADED)
        self.assertEqual(claimant.installed_commit(), installed)
        self.assertIn(f"loaded plugin {claimant.LOADED[:7]}, but {installed[:7]} is installed",
                      claimant.stale())
        self.assertTrue(claimant.identity().endswith(
            f"plugin {claimant.LOADED[:7]}, installed {installed[:7]}"))

    def test_the_new_folder_wins_over_the_one_before_the_rename(self):
        old = _checkout(self.plugins / "hermes-review-loop")
        self.assertEqual(claimant.installed_commit(), old, "only the old folder: it is the install")
        new = _checkout(self.plugins / "diaktoros")
        self.assertEqual(claimant.install_root(), self.plugins / "diaktoros")
        self.assertEqual(claimant.installed_commit(), new)

    def test_an_install_at_the_loaded_commit_is_not_stale(self):
        self.plugins.mkdir(parents=True)
        (self.plugins / "diaktoros").symlink_to(claimant.plugin_root(), target_is_directory=True)
        self.assertEqual(claimant.installed_commit(), claimant.LOADED)
        self.assertEqual(claimant.stale(), "")

    def test_an_install_folder_that_is_not_a_checkout_never_refuses(self):
        (self.plugins / "diaktoros").mkdir(parents=True)
        self.assertEqual(claimant.installed_commit(), "")
        self.assertEqual(claimant.stale(), "")

    def test_an_install_inside_another_repository_has_no_commit_of_its_own(self):
        # Review on #2572 (F3): git searches parent directories, so an unpacked install under a
        # HERMES_HOME that is itself in a checkout (a dotfiles home) read that repository's HEAD.
        host = pathlib.Path(tempfile.mkdtemp()) / "home"
        self.addCleanup(shutil.rmtree, host.parent, ignore_errors=True)
        _checkout(host)
        (host / "plugins" / "diaktoros").mkdir(parents=True)
        with mock.patch.object(config, "home", lambda: host):
            self.assertEqual(claimant.installed_commit(), "")
            self.assertEqual(claimant.stale(), "")
            installed = _checkout(host / "plugins" / "hermes-review-loop")  # a real checkout there
            shutil.rmtree(host / "plugins" / "diaktoros")
            self.assertEqual(claimant.installed_commit(), installed)

    def test_without_an_install_folder_the_loaded_checkout_is_the_install(self):
        self.assertEqual(claimant.install_root(), claimant.plugin_root())
        self.assertEqual(claimant.stale(), "")

    def test_the_loaded_commit_is_the_imported_checkouts_head(self):
        _checkout(self.plugins / "diaktoros")
        self.assertEqual(claimant.loaded_commit(), claimant.LOADED, "the install never moves it")
        self.assertEqual(claimant.loaded_commit(), claimant._head(claimant.plugin_root()))


if __name__ == "__main__":
    unittest.main()
