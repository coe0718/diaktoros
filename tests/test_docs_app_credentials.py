"""The general identity/security docs must not say PAT token files are required of every loop (#1543).

A loop made only of GitHub Apps has private keys and no PAT (docs/github-apps.md); the broker's
identity check (``broker._verify_identities``) accepts an App key as the identity's credential file.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def row(doc: str, start: str) -> str:
    """The one line of ``docs/<doc>`` that begins with ``start``, whitespace-normalised."""
    lines = [" ".join(line.split()) for line in (ROOT / "docs" / doc).read_text().splitlines()
             if line.startswith(start)]
    assert len(lines) == 1, (doc, start, lines)
    return lines[0]


class GeneralDocsNameAppKeys(unittest.TestCase):
    def test_concepts_login_row_allows_an_app_key(self):
        text = row("concepts.md", "| GitHub login |")
        self.assertIn("App", text)
        self.assertNotIn("mapped token files are required", text)

    def test_concepts_reader_row_allows_an_app_key(self):
        self.assertIn("App", row("concepts.md", "| Reader |"))

    def test_concepts_token_file_row_says_apps_have_none(self):
        text = row("concepts.md", "| Token file |")
        self.assertIn("App", text)
        self.assertIn("no PAT files", text)

    def test_concepts_credentials_row_lists_app_keys(self):
        self.assertIn("App", row("concepts.md", "| Credentials |"))

    def test_security_authorize_step_names_app_keys(self):
        text = row("security.md", "3. Explicit reader/reviewer/fixer mappings")
        self.assertIn("App", text)
        self.assertNotIn("distinct token files", text)

    def test_security_authorize_step_does_not_claim_user_for_app_tokens(self):
        # An installation token cannot call /user; gh.principal uses github_app.principal (/app,
        # /users/<slug>[bot]) for Apps and reserves /user for PATs.
        text = row("security.md", "3. Explicit reader/reviewer/fixer mappings")
        self.assertNotIn("called with an App's installation token", text)
        self.assertIn("cannot call `/user`", text)
        self.assertIn("`GET /app`", text)
        self.assertIn("`GET /users/<slug>[bot]`", text)

    def test_operations_token_section_points_apps_to_their_guide(self):
        ops = " ".join((ROOT / "docs" / "operations.md").read_text().split())
        section = ops.split("## Credential files: one per account", 1)[1].split("## ", 1)[0]
        self.assertIn("a PAT, or a GitHub App's private key", section)
        self.assertIn("github-apps.md", section)

    def test_operations_credential_section_is_kind_neutral_and_its_links_resolve(self):
        # #1687: the heading and its first rule held for every login, App logins included, and
        # the page's contents and the four-identity rule's message link to the new anchor.
        from diaktoros import config
        ops = (ROOT / "docs" / "operations.md").read_text()
        self.assertNotIn("one PAT per account", ops)
        self.assertNotIn("its own nonempty private token file", ops)
        self.assertIn("](#credential-files-one-per-account)", ops)
        self.assertIn("docs/operations.md#credential-files-one-per-account",
                      config.FOUR_IDENTITY_RULE)


class AppIdCapIsDocumented(unittest.TestCase):
    """#1748: the App id digit cap a user meets as a refusal is stated where the field is."""

    def test_settings_and_configuration_state_the_cap(self):
        from diaktoros import config
        cap = f"at most {config.APP_ID_MAX_DIGITS} digits"
        for role in ("reviewer", "fixer", "adjudicator"):
            self.assertIn(f"({cap})", row("settings.md", f"| `{role}_app_id` |"))
        self.assertIn(f"An App id is positive ASCII decimal ({cap})",
                      " ".join((ROOT / "docs" / "settings.md").read_text().split()))
        self.assertIn(f"positive App id of {cap}", row("configuration.md", "| `apps` |"))
        # The settings form shows plugin.yaml's description beside each App id field.
        self.assertEqual((ROOT / "plugin.yaml").read_text().count(
            f"Positive ASCII decimal App id ({cap}), paired with this seat's App key file"), 3)


class DocstringsQualifyUserChecks(unittest.TestCase):
    """#1687: an App identity resolves through ``/app`` and ``/users/<slug>[bot]``, never
    ``/user``, so no docstring says every identity resolves via ``/user``."""

    def test_principal_docstrings_name_the_app_path(self):
        from diaktoros import broker, broker_ipc, selftest
        from diaktoros.config import credentials
        from diaktoros.doctor import identity_checks
        for owner in (selftest, broker.authorize_ruling_comment,
                      credentials.verify_adjudicator_token, credentials.reader_problem,
                      identity_checks.check_adjudicator_identity,
                      broker_ipc.RunBroker._record_only):
            doc = " ".join((owner.__doc__ or "").split())
            with self.subTest(owner=getattr(owner, "__qualname__", owner.__name__)):
                self.assertIn("``/user`` for a PAT", doc)
                self.assertIn("``/app`` and ``/users/<slug>[bot]`` for a GitHub App", doc)
                self.assertNotIn("via ``/user``", doc)


class GeneralDocsQualifyUserChecks(unittest.TestCase):
    """Issue #1557: ``/user`` is the PAT path only; Apps resolve through ``github_app.principal``."""

    def flat(self, doc: str) -> str:
        return " ".join((ROOT / "docs" / doc).read_text().split())

    def assert_names_app_path(self, text: str):
        self.assertIn("`GET /app`", text)
        self.assertIn("`GET /users/<slug>[bot]`", text)
        self.assertIn("cannot call `/user`", text)

    def test_configuration_broker_check_names_app_path(self):
        text = self.flat("configuration.md")
        self.assertNotIn("live `/user` principals", text)
        part = text.split("Runtime broker checks additionally verify", 1)[1]
        self.assert_names_app_path(part.split("GitHub login matching", 1)[0])

    def test_accounts_selftest_row_names_app_path(self):
        self.assert_names_app_path(row("accounts.md", "| `selftest --no-model` GitHub identities"))

    def test_accounts_auth_failure_row_is_pat_only(self):
        text = row("accounts.md", "| `/user` authentication fails")
        self.assertIn("(PAT)", text)
        self.assertIn("App identity never calls `/user`", text)

    def test_commands_login_row_names_app_path(self):
        self.assert_names_app_path(row("commands.md", "| Login / allowlist options"))

    def test_settings_live_check_names_app_path(self):
        text = self.flat("settings.md")
        self.assertNotIn("Live `/user` checks belong", text)
        part = text.split("Live principal checks belong to", 1)[1]
        self.assert_names_app_path(part.split("See [Accounts]", 1)[0])


if __name__ == "__main__":
    unittest.main()
