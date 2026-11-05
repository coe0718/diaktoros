"""#757: ``operator_agents`` (logins that post as the operator, named apart from the operator) and
the authorship check (a commit that shows as a seat's account but that no seat turn pushed).
The behaviour of code that existed before ``authorship`` (an agent's comment and hold, what
``cmd_explain`` prints) is tested in ``test_operator_agents_explain.py``, which imports only that
code, so a fail-then-pass proof can run it on a head without this module (#1339).
GitHub is mocked; nothing real is touched."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import authorship, config, doctor, explain, gate, gh, pr_hold  # noqa: E402

STRAY = "c" * 40          # authored as the fixer's account; no seat turn pushed it
PUSHED = "d" * 40         # authored as the fixer's account; the fixer's turn pushed it
HUMAN = "e" * 40          # authored by someone who is no seat


def commit(sha, login, message="change"):
    return {"sha": sha, "author": {"login": login} if login else None,
            "commit": {"message": message}}


COMMITS = [commit(PUSHED, "Fixer", "fix: pushed by the turn"),
           commit(STRAY, "fixer", "docs: written by another agent"),
           commit(HUMAN, "someone"), commit("f" * 40, None)]


class OperatorAgentsConfig(fg.Base):
    def load(self, **extra):
        raw = {**fg.raw_loop(False), "state_dir": str(self.root / "state"), **extra}
        return config.normalize(raw)

    def test_logins_are_lowercased_and_default_empty(self):
        self.assertEqual(self.load()["operator_agents"], [])
        self.assertEqual(self.load(operator_agents=["Docs-Bot", "bug-hunter"])["operator_agents"],
                         ["docs-bot", "bug-hunter"])

    def test_a_seat_account_cannot_be_an_operator_agent(self):
        for login in ("fixer", "reviewer"):
            with self.assertRaises(config.ConfigError) as cm:
                self.load(operator_agents=[login])
            self.assertIn("operator agent", str(cm.exception))

    def test_a_string_or_a_duplicate_is_refused(self):
        for bad in ("docs-bot", ["a", "a"], [3]):
            with self.assertRaises(config.ConfigError):
                self.load(operator_agents=bad)

    def test_agents_are_maintainers_without_being_in_triage_maintainers(self):
        loop = self.load(operator_agents=["docs-bot"])
        self.assertEqual(config.maintainers(loop), {"docs-bot"})
        self.assertEqual(config.maintainer_kind(loop, "Docs-Bot"), "operator agent")
        self.assertEqual(config.maintainer_kind(loop, "boss"), "maintainer")


class Authorship(fg.Base):
    def journal(self, *records):
        path = config.state_dir(self.loop) / "broker-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def push_record(self, new_head):
        return {"repo": fg.REPO, "pr": 7, "role": "fixer", "operation": "push",
                "phase": "reconciled", "new_head": new_head, "head": fg.HEAD}

    def test_only_a_seat_attributed_commit_with_no_push_is_reported(self):
        self.journal(self.push_record(PUSHED))
        pushed, error = authorship.pushed_heads(self.loop)
        self.assertEqual(error, "")
        found = authorship.misattributed(self.loop, COMMITS, pushed)
        self.assertEqual([entry["sha"] for entry in found], [STRAY])
        self.assertEqual(found[0]["login"], "fixer")

    def test_a_review_of_a_commit_does_not_make_it_look_pushed(self):
        # The audit's ``head`` is the PR head a write was about, not a commit the loop made.
        self.journal({**self.push_record(PUSHED), "new_head": None, "head": STRAY})
        pushed, _ = authorship.pushed_heads(self.loop)
        self.assertEqual([e["sha"] for e in authorship.misattributed(self.loop, COMMITS, pushed)],
                         [PUSHED, STRAY])

    def test_a_missing_journal_is_no_pushes_but_an_unreadable_one_is_an_error(self):
        self.assertEqual(authorship.pushed_heads(self.loop), (set(), ""))
        path = config.state_dir(self.loop) / "broker-audit.jsonl"
        path.mkdir(parents=True)
        self.assertNotEqual(authorship.pushed_heads(self.loop)[1], "")

    def explain_report(self):
        self.journal(self.push_record(PUSHED))
        pr = {**fg.LIVE, "mergeable_state": "clean"}
        with mock.patch.object(gh, "fetch", return_value=(pr, "")), \
             mock.patch.object(gh, "reviews_read", return_value=([], "")), \
             mock.patch.object(gate, "hooks_read", return_value=(True, "")), \
             mock.patch.object(pr_hold, "read", return_value=(None, "")), \
             mock.patch.object(gh, "pr_commits_read", return_value=(COMMITS, "")):
            facts = explain.explain_facts(self.loop, 7)
        return explain.explain(self.loop, self.st, 7, facts)

    def test_explain_reports_it_and_does_not_block(self):
        report = self.explain_report()
        self.assertEqual(len(report["authorship"]), 1)
        self.assertIn(STRAY[:7], report["authorship"][0])
        self.assertIn("misattributed", report["authorship"][0])
        self.assertFalse(any(STRAY[:7] in b for b in report["blockers"]))      # never a blocker

    def test_an_unreadable_commit_list_is_said_not_taken_as_clean(self):
        with mock.patch.object(gh, "pr_commits_read", return_value=(None, "HTTP 502")):
            found, error = authorship.scan(self.loop, 7, set())
        self.assertEqual(found, [])
        self.assertIn("unreadable", error)

    def doctor_check(self, loop=None, offline=False):
        with mock.patch.object(gh, "open_prs_read", return_value=([{"number": 7}], "")), \
             mock.patch.object(gh, "pr_commits_read", return_value=(COMMITS, "")):
            return doctor.check_authorship(loop or self.loop, offline)

    def test_doctor_flags_it_as_a_warning_not_a_failure(self):
        self.journal(self.push_record(PUSHED))
        check = self.doctor_check()
        self.assertEqual(check.status, doctor.UNKNOWN)
        self.assertFalse(check.failed)
        self.assertIn(f"#7 commit {STRAY[:7]}", check.detail)
        self.assertNotIn(PUSHED[:7], check.detail)

    def test_doctor_is_clean_when_every_seat_commit_was_pushed(self):
        self.journal(self.push_record(PUSHED), self.push_record(STRAY))
        self.assertEqual(self.doctor_check().status, doctor.VERIFIED)

    def test_doctor_names_the_operator_agents(self):
        loop = config.normalize({**fg.raw_loop(False), "state_dir": str(self.root / "state"),
                                 "operator_agents": ["docs-bot"]})
        self.journal(self.push_record(PUSHED), self.push_record(STRAY))
        self.assertIn("operator agents", self.doctor_check(loop).detail)
        self.assertIn("docs-bot", self.doctor_check(loop, offline=True).detail)

    def test_check_loop_includes_the_check(self):
        with mock.patch.object(doctor, "check_authorship",
                               return_value=doctor.Check("authorship", doctor.VERIFIED, "x")) as c:
            names = [k.name for k in self.check_loop()]
        c.assert_called_once()
        self.assertIn("authorship", names)

    def check_loop(self):
        # Every other check is stubbed out: this asks only whether ``check_loop`` calls ours. The
        # checks live in the core and its submodules (#1349); each is stubbed where it lives.
        from diaktoros.doctor import (cron_checks, hook_checks, identity_checks, route_checks,
                                      seat_checks)
        modules = (doctor, hook_checks, route_checks, identity_checks, seat_checks, cron_checks)
        stubs = [(module, name) for module in modules for name in vars(module)
                 if name.startswith("check_") and name not in ("check_loop", "check_authorship")]
        with contextlib.ExitStack() as stack:
            for module, name in stubs:
                if callable(getattr(module, name)):
                    stack.enter_context(mock.patch.object(
                        module, name, return_value=doctor.Check(name, doctor.VERIFIED, "")))
            # Families that return lists.
            for name in ("check_triage", "check_seat_models", "check_seat_extras", "check_tokens",
                         "check_routes", "check_gateway_scripts", "check_gate_timeouts",
                         "check_runtime_paths", "check_hooks", "check_environment"):
                module = next(module for module in modules if name in vars(module))
                stack.enter_context(mock.patch.object(module, name, return_value=[]))
            stack.enter_context(mock.patch.object(doctor, "check_review_only_update",
                                                  return_value=None))
            stack.enter_context(mock.patch.object(identity_checks, "check_adjudicator_identity",
                                                  return_value=None))
            stack.enter_context(mock.patch.object(doctor, "check_duplicate_repo",
                                                  return_value=None))
            return doctor.check_loop(self.loop, True)


if __name__ == "__main__":
    unittest.main()
