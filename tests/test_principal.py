"""One place resolves who a credential is (#1163 M1a): ``gh.principal``.

Every seat and reader identity check (the broker's authorizers, the review receipt, safe_push's
fixer identity, trusted_fetch) asks ``gh.principal``, each with its own transport. A GitHub App
identity cannot call ``GET /user`` with its installation token, so the App path (M1b) has one
function to change; a new ``/user`` call elsewhere would bypass it. The fixer's git identity
accepts a GitHub App bot login (``name[bot]``) and its noreply address.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, gh, safe_push  # noqa: E402

# The only modules that may still name /user: the seam itself, and the read-only diagnostics
# that read a PAT's own response headers (expiry, scopes), which M1b makes App-aware.
USER_CALLERS = {"diaktoros/gh.py", "diaktoros/doctor.py", "diaktoros/selftest.py"}


class Seam(unittest.TestCase):
    def test_principal_returns_the_account_or_none(self):
        with mock.patch.object(gh, "api", return_value={"login": "fix", "id": 7}) as api:
            self.assertEqual(gh.principal({}, "fix"), {"login": "fix", "id": 7})
        api.assert_called_once_with({}, "/user", login="fix")
        for answer in (None, [], "Bad credentials"):
            with self.subTest(answer=answer), mock.patch.object(gh, "api", return_value=answer):
                self.assertIsNone(gh.principal({}, "fix"))

    def test_the_callers_transport_is_used_and_its_failure_kept(self):
        seen = []
        self.assertEqual(gh.principal({}, "fix", fetch=lambda path: seen.append(path) or {"id": 1}),
                         {"id": 1})
        self.assertEqual(seen, ["/user"])

        def raises(path):
            raise broker.BrokerDenied("GitHub read request failed")
        with self.assertRaisesRegex(broker.BrokerDenied, "GitHub read request failed"):
            gh.principal({}, "fix", fetch=raises)

    def test_no_identity_check_calls_user_directly(self):
        found = []
        for path in sorted((ROOT / "diaktoros").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in USER_CALLERS:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"""["']/user["']""", line):
                    found.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual(found, [], "resolve identities through gh.principal")


class FixerIdentity(unittest.TestCase):
    def identity(self, account, login="fix", **kw):
        with mock.patch.object(safe_push, "_api", return_value=account):
            return safe_push._fixer_identity({}, login, **kw)

    def test_a_user_and_an_app_bot_get_their_noreply_address(self):
        self.assertEqual(self.identity({"login": "fix", "id": 9}),
                         {"name": "fix", "email": "9+fix@users.noreply.github.com"})
        self.assertEqual(self.identity({"login": "fix-bot[bot]", "id": 346015539},
                                       login="fix-bot[bot]"),
                         {"name": "fix-bot[bot]",
                          "email": "346015539+fix-bot[bot]@users.noreply.github.com"})

    def test_another_account_or_a_bad_id_is_refused(self):
        for account in ({"login": "other", "id": 9}, {"login": "fix", "id": 0},
                        {"login": "fix", "id": True}, {"login": "fix"}, {"id": 9}):
            with self.subTest(account=account), \
                    self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                self.identity(account)

    def test_a_login_git_would_not_take_is_refused_with_the_callers_reason(self):
        for name in ("fix bot", "fix[bot]x", "fix[robot]", "fix;rm"):
            with mock.patch.object(safe_push, "_api", return_value={"login": name, "id": 9}):
                with self.subTest(name=name), \
                        self.assertRaisesRegex(broker.BrokerDenied, "invalid fixer identity"):
                    safe_push._fixer_identity({}, name)
                with self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                    safe_push._fixer_identity({}, name, invalid="fixer identity changed")


if __name__ == "__main__":
    unittest.main()
