"""#1196: a GitHub App identity can be --admin-token. The --admin-token checks of init/setup,
uninstall, triage and trace only accepted a PAT-mapped login, while the hook path itself already
mints an App installation token; and the --token fix they printed was then refused for an App."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros.cli import common as cli_common  # noqa: E402

LOOP = {"id": "w", "tokens": {"reader": "/t/reader.pat"},
        "apps": {"admin[bot]": {"app_id": 9, "key_file": "/k/admin.pem"}}}


class AdminUnmapped(unittest.TestCase):
    def test_a_pat_or_an_app_is_mapped(self):
        self.assertEqual(cli_common.admin_unmapped(LOOP, "reader"), "")
        self.assertEqual(cli_common.admin_unmapped(LOOP, "Admin[bot]"), "")   # any case
        self.assertEqual(cli_common.admin_unmapped(LOOP, ""), "")

    def test_an_unmapped_login_names_both_ways_to_map_it(self):
        why = cli_common.admin_unmapped(LOOP, "stranger")
        self.assertIn("--admin-token 'stranger' has no credential mapped", why)
        self.assertIn("--token stranger=/abs/path/to/pat", why)
        self.assertIn("--app stranger=APP_ID:/abs/path/to/key.pem", why)
        self.assertIn("Webhooks: Read and write", why)

    def test_a_login_this_command_maps_is_accepted(self):
        self.assertEqual(cli_common.admin_unmapped(LOOP, "new-admin", also={"new-admin"}), "")



class HookWriteNeed(unittest.TestCase):
    """#1276: the hook-write remedy for an App admin names the App key and its permission, as
    admin_unmapped does, not a PAT's scopes and token file."""
    LOOP = dict(LOOP, repo="acme/widgets", read_token="reader")

    def test_an_app_admin_gets_the_app_remedy(self):
        need = cli_common.hook_write_need(self.LOOP, "admin[bot]")
        self.assertIn("the App key for 'admin[bot]' needs hook write access on acme/widgets", need)
        self.assertIn("Webhooks: Read and write", need)
        for pat_only in ("token file", "admin:repo_hook", "repository_hooks", "classic"):
            self.assertNotIn(pat_only, need)

    def test_a_pat_admin_keeps_the_pat_remedy(self):
        need = cli_common.hook_write_need(dict(self.LOOP, tokens={"reader": "/t/r", "pat": "/t/p"}),
                                          "pat")
        self.assertIn("the token for 'pat' needs hook write access", need)
        self.assertIn("repository_hooks: write", need)
        self.assertIn("check that login's token file", need)

    def test_init_hooks_line_for_an_app_admin(self):
        from diaktoros.cli import install
        line = install._hook_editor_line(self.LOOP, "admin[bot]", False, True)
        self.assertIn("Webhooks: Read and write", line)
        self.assertNotIn("token file", line)


class Commands(unittest.TestCase):
    def test_trace_refuses_an_unmapped_admin_and_accepts_an_app(self):
        import argparse
        import contextlib
        import io
        from unittest import mock
        from diaktoros.cli import reports
        for admin, refused in (("stranger", True), ("admin[bot]", False)):
            with self.subTest(admin=admin), \
                    mock.patch.object(reports.config, "load_id", return_value=LOOP), \
                    mock.patch("diaktoros.trace.fetch_delivery",
                               side_effect=RuntimeError("past the check")), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    rc = reports.cmd_trace(argparse.Namespace(loop="w", admin_token=admin,
                                                              guid="g", event="", route=""))
                except (RuntimeError, AttributeError):
                    rc = None        # got past the admin check (the stub args stop it after)
            if refused:
                self.assertEqual(rc, 2)
                self.assertIn("no credential mapped", out.getvalue())
            else:
                self.assertNotIn("no credential mapped", out.getvalue())

if __name__ == "__main__":
    unittest.main()
