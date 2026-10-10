"""#577: the fixer is told which registries its change must also update.

Most of the fixer's red CI was a registry test: a new command, event, doctor check or setting
not added to every list that names them. A fix round gets the rows its PR's diff matches; an
issue fix, a CI fix and a conflict turn (no diff to read yet) get every row.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, gh, issue_facts, prompts, run_supervisor  # noqa: E402

LOOP = {"id": "w", "repo": "acme/w", "base": "main", "cap": 3, "read_token": "read",
        "seats": {}, "fixers": ["fix"], "reviewers": ["rev"], "reviewer_seat": "rev"}


def diff(path, *added):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,9 @@\n"
            + "".join(f"+{line}\n" for line in added))


DIFFS = {
    "command": diff("diaktoros/cli.py", "    p = sub.add_parser('wave', help='wave')"),
    "event": diff("diaktoros/observer.py", "    'waved',"),
    "doctor_check": diff("diaktoros/doctor.py", '    return Check("wave", VERIFIED, "ok")'),
    "setting": diff("diaktoros/config.py", '    "wave": {"label": "Wave", "type": "bool"},'),
    "setting_yaml": diff("plugin.yaml", "  wave:"),
    "broker_refusal": diff("diaktoros/broker.py", "        raise ProtocolError('no wave')"),
}
TEXT = {kind: row[1] for kind, row in prompts.REGISTRY_ROWS.items()}


class Detection(unittest.TestCase):
    def test_each_kind_shows_its_row_and_no_other(self):
        for name, text in DIFFS.items():
            kind = "setting" if name == "setting_yaml" else name
            with self.subTest(name):
                section = prompts.registry_section(text)
                self.assertIn(TEXT[kind], section)
                for other in set(TEXT) - {kind}:
                    self.assertNotIn(TEXT[other], section)

    def test_an_unrelated_diff_gets_no_section(self):
        self.assertEqual(prompts.registry_section(diff("README.md", "Waves.")), "")

    def test_a_removed_line_is_not_an_addition(self):
        removed = diff("diaktoros/cli.py").replace(
            "@@ -1 +1,9 @@\n", "@@ -1 +1,9 @@\n-    sub.add_parser('wave')\n")
        self.assertEqual(prompts.registry_section(removed), "")

    def test_no_diff_shows_every_row(self):
        section = prompts.registry_section(None)
        for text in TEXT.values():
            self.assertIn(text, section)
        self.assertIn("If your change adds any of these", section)


class Prompts(unittest.TestCase):
    def fix_round(self, change):
        row = {"seat": "fixer", "repo": "acme/w", "pr": 7, "head": "a" * 40}
        with mock.patch.object(ci, "read", return_value=ci.CIState()), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(gh, "pr_url", return_value="https://github.com/acme/w/pull/7"), \
                mock.patch("diaktoros.gate.verdicts", return_value=[{}]), \
                mock.patch("diaktoros.gate.latest_effective_review_at_head", return_value=None):
            return run_supervisor.isolated_prompt(LOOP, row, [], change=change)

    def test_a_fix_round_gets_only_the_rows_its_diff_matches(self):
        text = self.fix_round(run_supervisor.PRChange("RECORD", DIFFS["event"]))
        self.assertIn(TEXT["event"], text)
        self.assertNotIn(TEXT["command"], text)

    def test_a_fix_round_on_an_unrelated_diff_has_no_checklist(self):
        text = self.fix_round(run_supervisor.PRChange("RECORD", diff("README.md", "Waves.")))
        self.assertNotIn("## Registries", text)

    def test_an_issue_fix_gets_every_row(self):
        issue = {"number": 5, "state": "open", "user": {"login": "o"}, "title": "T5",
                 "body": "BODY5", "labels": [], "html_url": "https://github.com/acme/w/issues/5"}
        with mock.patch.object(run_supervisor, "issue_fix_issue", return_value=issue), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")):
            text = run_supervisor.issue_fix_prompt(LOOP, {"repo": "acme/w", "pr": 5,
                                                          "head": "c" * 40})
        self.assertIn(prompts.registry_section(None), text)

    def test_a_conflict_turn_gets_every_row(self):
        row = {"turn_key": run_supervisor.CONFLICT_KEY + "b" * 40, "repo": "acme/w", "pr": 7,
               "head": "a" * 40}
        with mock.patch.object(gh, "pr_url", return_value="https://github.com/acme/w/pull/7"):
            text = run_supervisor.conflict_prompt(LOOP, row, {"conflicted": ["x.py"]})
        self.assertIn(prompts.registry_section(None), text)

    def test_the_ci_fix_turn_uses_the_shared_text(self):
        # The CI-fix prompt is built inline in the worker; pin that it appends the shared text.
        src = Path(run_supervisor.__file__).read_text()
        block = src[src.index("render_isolated('ci_fix'"):][:600]
        self.assertIn("prompts.registry_section(None)", block)


if __name__ == "__main__":
    unittest.main()
