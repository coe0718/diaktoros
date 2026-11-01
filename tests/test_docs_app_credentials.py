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
        section = ops.split("## Token files: one PAT per account", 1)[1].split("## ", 1)[0]
        self.assertIn("GitHub App has a private key file", section)
        self.assertIn("github-apps.md", section)


if __name__ == "__main__":
    unittest.main()
