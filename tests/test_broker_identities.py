"""Duplication audit (C2): the broker's identity and live-PR checks, one rule for every writer.

``authorize`` (the seats), ``authorize_ruling_comment`` (the adjudicator), ``authorize_triage`` and
``authorize_issue_fix`` each spelled out "distinct logins, mapped and distinct credential files, a
token that is there, a principal that is that very account, distinct principals", and the first
two the live-PR identity checks too. The copies drifted: ``authorize`` compared logins case-
sensitively, and triage and issue fixes never asked for distinct principals. Each rule is pinned
here for every writer that has it, before the checks share one home.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import broker, config, gh  # noqa: E402
from diaktoros.run_supervisor import issue_inputs  # noqa: E402

REPO = "acme/widgets"
HEAD = "a" * 40
IDS = {"read": 1, "review": 2, "fix": 3, "adj": 4, "triager": 5}


class Identities(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        self.loop = {"id": "w", "repo": REPO, "base": "main", "cap": 3, "fixers": ["fix"],
                     "reviewers": ["review"], "read_token": "read", "reviewer_seat": "review",
                     "tokens": {name: str(root / f"{name}.pat") for name in IDS},
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"},
                               "adjudicator": {"login": "adj"}},
                     "adjudicator": {"route": "w-breach"},
                     "triage": {"route": "w-triage", "login": "triager"}}
        self.tokens = {name: f"DUMMY_{name}" for name in IDS}
        self.principals = {name: {"login": name, "id": number} for name, number in IDS.items()}
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                   "base": {"ref": "main", "repo": {"full_name": REPO}},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": REPO}}}
        for target, name, kwargs in (
                (gh, "token", {"side_effect": lambda loop, login=None: self.tokens[login]}),
                (gh, "principal", {"side_effect": lambda loop, login, fetch=None:
                                   self.principals.get(login)}),
                (gh, "api", {"side_effect": lambda loop, path, login=None: self.pr}),
                (gh, "reviews", {"return_value": []}),
                (config, "issue_fixes_enabled", {"return_value": True}),
                (issue_inputs, "triage_issue", {"return_value": {}}),
                (issue_inputs, "issue_fix_issue", {"return_value": {}}),
                (issue_inputs, "issue_fix_dependencies", {"return_value": None})):
            patch = mock.patch.object(target, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    # Each writer, and the identities whose token, principal and distinct id it checks.
    WRITERS = {
        "seat": (lambda loop: broker.authorize(loop, repo=REPO, number=7, head=HEAD,
                                               role="reviewer", branch="fix-7", operation="review"),
                 ("read", "review", "fix")),
        "ruling": (lambda loop: broker.authorize_ruling_comment(loop, repo=REPO, number=7,
                                                                head=HEAD, branch="fix-7"),
                   ("read", "review", "fix", "adj")),
        "triage": (lambda loop: broker.authorize_triage(loop, repo=REPO, number=7),
                   ("read", "triager")),
        "issue fix": (lambda loop: broker.authorize_issue_fix(loop, repo=REPO, number=7),
                      ("read", "review", "fix")),
    }

    def denied(self, writer):
        with self.assertRaises(broker.BrokerDenied) as caught:
            self.WRITERS[writer][0](self.loop)
        return str(caught.exception)

    def test_each_writer_passes_with_every_identity_in_place(self):
        expected = {"seat": "review", "ruling": "adj", "triage": "triager", "issue fix": "fix"}
        for writer, (call, _) in self.WRITERS.items():
            with self.subTest(writer):
                self.assertEqual(call(self.loop), expected[writer])

    def test_a_login_shared_with_another_identity_in_any_case_is_denied(self):
        # The seat writer compared logins exactly, so FIX and fix counted as two accounts.
        for writer, (_, identities) in self.WRITERS.items():
            other = identities[-1]
            with self.subTest(writer):
                self.setUp()
                seat = {"seat": "fixer", "ruling": "adjudicator", "issue fix": "fixer"}.get(writer)
                if seat:
                    self.loop["seats"][seat]["login"] = "READ"
                    self.loop["tokens"]["READ"] = self.loop["tokens"].pop(other)
                    self.principals["READ"] = {"login": "READ", "id": IDS[other]}
                    self.tokens["READ"] = "DUMMY_upper"
                else:
                    self.loop["triage"]["login"] = "READ"
                self.assertIn("distinct", self.denied(writer))

    def test_an_unmapped_or_shared_credential_file_is_denied(self):
        for writer, (_, identities) in self.WRITERS.items():
            for case in ("unmapped", "shared file"):
                with self.subTest(writer=writer, case=case):
                    self.setUp()
                    if case == "unmapped":
                        del self.loop["tokens"][identities[-1]]
                    else:
                        self.loop["tokens"][identities[-1]] = self.loop["tokens"]["read"]
                    self.denied(writer)

    def test_an_empty_or_missing_token_or_a_foreign_principal_is_denied(self):
        # Every identity a writer depends on, not only the one it writes as (#1487 review): the
        # reader's and the reviewer's tokens are checked too.
        def missing(who):
            gh.token.side_effect = (lambda loop, login=None: (_ for _ in ()).throw(
                gh.GitHubError("gone")) if login == who else self.tokens[login])
        cases = {"empty token": lambda who: self.tokens.update({who: ""}),
                 "missing token": missing,
                 "another account": lambda who: self.principals.update(
                     {who: {"login": "someone", "id": 99}}),
                 "no id": lambda who: self.principals.update({who: {"login": who}}),
                 "unreadable": lambda who: self.principals.update({who: None})}
        for writer, (_, identities) in self.WRITERS.items():
            for who in identities:
                for case, mutate in cases.items():
                    with self.subTest(writer=writer, identity=who, case=case):
                        self.setUp()
                        mutate(who)
                        self.denied(writer)

    def test_two_identities_resolving_to_one_principal_are_denied(self):
        # Triage and issue fixes checked only their own principal, so a writer token that is
        # really the reader's account passed (the four-identity rule).
        for writer, (_, identities) in self.WRITERS.items():
            with self.subTest(writer):
                self.setUp()
                mine = identities[-1]
                self.principals[mine] = {"login": mine, "id": IDS["read"]}
                self.assertIn("principal", self.denied(writer))


class LivePR(Identities):
    """The seat and ruling writers re-read the PR as the reader right before writing."""

    def test_a_pr_that_moved_is_denied_by_each_writer_that_reads_it(self):
        cases = {"closed": {"state": "closed"}, "draft": {"draft": True},
                 "draft unknown": {"draft": None}, "another number": {"number": 8},
                 "base repo": {"base": {"ref": "main", "repo": {"full_name": "x/y"}}},
                 "base branch": {"base": {"ref": "dev", "repo": {"full_name": REPO}}},
                 "stale head": {"head": {"sha": "b" * 40, "ref": "fix-7",
                                         "repo": {"full_name": REPO}}},
                 "branch": {"head": {"sha": HEAD, "ref": "other", "repo": {"full_name": REPO}}},
                 "fork": {"head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": "x/y"}}},
                 "stranger's PR": {"user": {"login": "someone"}}}
        for writer in ("seat", "ruling"):
            for case, change in cases.items():
                with self.subTest(writer=writer, case=case):
                    self.setUp()
                    self.pr.update(change)
                    if writer == "seat" and case == "stranger's PR":
                        continue        # a reviewer may review any loop PR it was routed
                    self.denied(writer)
            with self.subTest(writer=writer, case="unreadable"):
                self.setUp()
                self.pr = None
                self.denied(writer)

    test_each_writer_passes_with_every_identity_in_place = None
    test_a_login_shared_with_another_identity_in_any_case_is_denied = None
    test_an_unmapped_or_shared_credential_file_is_denied = None
    test_an_empty_or_missing_token_or_a_foreign_principal_is_denied = None
    test_two_identities_resolving_to_one_principal_are_denied = None


if __name__ == "__main__":
    unittest.main()
