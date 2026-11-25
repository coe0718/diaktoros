"""#515: a maintainer's PR review bodies and inline review comments reach the seats through the
shared reader (``issue_facts.section``), a stranger's never; the conflict and CI-fix prompts
carry the same section; every seat prompt builder that carries PR or issue text calls it."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import inspect
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import gh, issue_facts, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

LOOP = {"repo": "acme/w", "id": "l", "base": "main", "read_token": "read", "cap": 3,
        "seats": {}, "reviewers": ["rev"], "triage": {"maintainers": ["Boss"]}}


def who(login):
    return {"login": login}


def api(loop, path, **kw):
    if path.endswith("/pulls/7"):
        return {"number": 7, "body": "Fixes #5"}
    if path.endswith("/issues/5"):
        return {"number": 5, "title": "T5", "body": "BODY5"}
    return None


def issue_comments(loop, number):
    return [], ""


def reviews(loop, number, login=None):
    return [{"user": who("boss"), "state": "COMMENTED", "body": "MAINT-REVIEW-BODY",
             "submitted_at": "2026-01-02T00:00:00Z"},
            {"user": who("stranger"), "state": "COMMENTED", "body": "STRANGER-REVIEW",
             "submitted_at": "2026-01-02T00:00:00Z"}], ""


def inline(loop, number):
    return [{"user": who("boss"), "path": "a.py", "line": 3, "body": "MAINT-INLINE",
             "created_at": "2026-01-03T00:00:00Z"},
            {"user": who("stranger"), "path": "a.py", "line": 4, "body": "STRANGER-INLINE",
             "created_at": "2026-01-03T00:00:00Z"}], ""


class Base(unittest.TestCase):
    def setUp(self):
        for name, fn in (("api", api), ("issue_comments_read", issue_comments),
                         ("reviews_read", reviews), ("review_comments_read", inline),
                         ("pr_url", lambda loop, n: "https://github.com/acme/w/pull/7")):
            patch = mock.patch.object(gh, name, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)

    def assert_maintainers_only(self, text):
        self.assertIn("MAINT-REVIEW-BODY", text)
        self.assertIn("MAINT-INLINE", text)
        self.assertIn("`a.py` line 3", text)
        self.assertNotIn("STRANGER-REVIEW", text)
        self.assertNotIn("STRANGER-INLINE", text)


class ReviewBodies(Base):
    def test_section_shows_maintainer_review_bodies_and_inline_comments_only(self):
        self.assert_maintainers_only(issue_facts.section(LOOP, 7))

    def test_conflict_prompt_carries_the_maintainer_section(self):
        row = {"turn_key": run_supervisor.CONFLICT_KEY + "b" * 40, "repo": "acme/w", "pr": 7,
               "head": "a" * 40}
        self.assert_maintainers_only(turn_prompts.conflict_prompt(LOOP, row, {"conflicted": ["x.py"]}))

    def test_ci_fix_prompt_carries_the_maintainer_section(self):
        row = {"repo": "acme/w", "pr": 7, "head": "a" * 40}
        with mock.patch("diaktoros.ci_fix.section", return_value="\nJOBS"):
            text = turn_prompts.ci_fix_prompt(LOOP, row, None, ["test"])
        self.assert_maintainers_only(text)
        self.assertTrue(text.endswith("JOBS"))


class LoopVerdicts(Base):
    def test_a_maintainer_who_is_also_a_reviewer_login_is_shown(self):
        # #515 review F1: a human maintainer may be in ``reviewers`` too (config allows it).
        # Their own review (no loop footer) is shown; the loop's signed verdict by the same
        # login is not (it is in the PR record already).
        from diaktoros import attribution
        loop = {**LOOP, "reviewers": ["boss"]}
        signed = attribution.stamp(loop, "LOOP-VERDICT", seat="reviewer", head="a" * 40)
        got = [{"user": who("Boss"), "state": "COMMENTED", "body": "BOSS-OWN-REVIEW",
                "submitted_at": "2026-01-02T00:00:00Z"},
               {"user": who("boss"), "state": "CHANGES_REQUESTED", "body": signed,
                "submitted_at": "2026-01-02T00:00:01Z"}], ""
        with mock.patch.object(gh, "reviews_read", return_value=got):
            text = issue_facts.section(loop, 7)
        self.assertIn("BOSS-OWN-REVIEW", text)
        self.assertNotIn("LOOP-VERDICT", text)


class PartialRead(Base):
    def test_one_unreadable_source_is_named_and_the_rest_still_shown(self):
        # #515 review F4: with comments shown, the note naming the failed read stays visible.
        with mock.patch.object(gh, "reviews_read", return_value=(None, "boom")):
            text = issue_facts.section(LOOP, 7)
        self.assertIn("MAINT-INLINE", text)
        self.assertIn("(The maintainers' review bodies on the PR could not be read for this "
                      "turn.)", text)
        self.assertNotIn("inline review comments on the PR could not", text)


class InlineCommentsRead(unittest.TestCase):
    """#515 review F5: the real ``gh.review_comments_read`` through ``gh.fetch``."""

    def read(self, pages):
        asked = []

        def fetch(loop, path, *a, **k):
            asked.append(path)
            page = int(path.split("&page=")[1]) if "&page=" in path else 1
            answer = pages[page - 1] if page <= len(pages) else []
            return (None, answer) if isinstance(answer, str) else (answer, "")
        with mock.patch.object(gh, "fetch", side_effect=fetch):
            return gh.review_comments_read(LOOP, 7), asked

    def test_every_page_is_read_from_the_pulls_comments_endpoint(self):
        first = [{"id": i, "user": who("stranger"), "body": "x"} for i in range(100)]
        second = [{"id": 100, "user": who("boss"), "path": "a.py", "line": 9,
                   "body": "LATE-PAGE", "created_at": "2026-01-05T00:00:00Z"}]
        (got, why), asked = self.read([first, second])
        self.assertEqual(why, "")
        self.assertEqual(asked[0], "/repos/acme/w/pulls/7/comments?per_page=100")
        self.assertEqual(asked[1], "/repos/acme/w/pulls/7/comments?per_page=100&page=2")
        self.assertEqual(len(got), 101)
        items = issue_facts.maintainers_among(LOOP, issue_facts._inline_items(got), None)
        self.assertEqual([body for _, _, body in items],
                         ["[inline review comment on `a.py` line 9]\nLATE-PAGE"])

    def test_a_failed_page_makes_the_listing_unread(self):
        first = [{"id": i, "user": who("boss"), "body": "x"} for i in range(100)]
        (got, why), _ = self.read([first, "HTTP 502"])
        self.assertIsNone(got)
        self.assertTrue(why)


class TriagePrompt(unittest.TestCase):
    def test_triage_carries_the_maintainers_comments_on_the_issue(self):
        # #515 review F2: the triage turn is given issue text, so it gets the shared section.
        loop = {**LOOP, "triage": {"maintainers": ["boss"], "labels": ["bug"], "route": "t"}}
        comments = [{"user": who("boss"), "body": "MAINT-ON-ISSUE", "created_at": "2026-01-01"},
                    {"user": who("stranger"), "body": "STRANGER-ON-ISSUE",
                     "created_at": "2026-01-01"}]
        with mock.patch("diaktoros.run_supervisor.issue_inputs.triage_issue",
                        return_value={"number": 12, "title": "T", "body": "B"}), \
                mock.patch.object(gh, "labels_read", return_value=(None, "x")), \
                mock.patch.object(gh, "issue_comments_read", return_value=(comments, "")):
            text = turn_prompts.triage_prompt(loop, {"repo": "acme/w", "pr": 12})
        self.assertIn("MAINT-ON-ISSUE", text)
        self.assertNotIn("STRANGER-ON-ISSUE", text)


class FixerAnswersMaintainerPoints(unittest.TestCase):
    def test_the_fix_round_prompt_asks_for_an_answer_to_each_maintainer_point(self):
        # #515 review F6 (the issue's "the fixer's answers include the maintainer points").
        from diaktoros import prompts
        text = " ".join(prompts.ISOLATED_FIXER.split())
        self.assertIn("Answer each point in the maintainers' comments and reviews on this PR",
                      text)
        self.assertIn("`Maintainers' points` heading", text)


ROOT = Path(__file__).resolve().parents[1]
READERS = ("issue_facts.section", "issue_facts.comments_section")


def _called(node) -> set[str]:
    """Dotted names (``a.b``) and bare names called anywhere inside ``node``."""
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                out.add(f"{fn.value.id}.{fn.attr}")
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
            elif isinstance(fn, ast.Name):
                out.add(fn.id)
    return out


def builders(source: str, where: str) -> dict[str, bool]:
    """Every function in ``source`` that renders a seat prompt (calls ``render_isolated``),
    keyed ``where::name``, mapped to whether it calls the shared reader."""
    out = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name != "render_isolated":
            calls = _called(node)
            if "prompts.render_isolated" in calls or "render_isolated" in calls:
                out[f"{where}::{node.name}"] = any(r in calls for r in READERS)
    return out


def scan() -> dict[str, bool]:
    """``builders`` over every module of the package, so a builder anywhere is found."""
    found = {}
    for path in sorted((ROOT / "diaktoros").rglob("*.py")):
        found.update(builders(path.read_text(), str(path.relative_to(ROOT))))
    return found


class EveryBuilderCallsTheReader(unittest.TestCase):
    # Every seat prompt builder that carries PR or issue text: every function in the package
    # that renders an isolated seat prompt (``prompts.render_isolated``, the one renderer every
    # seat turn's prompt goes through). A new one is found by the scan and fails until it is
    # listed here, and it must call the shared reader.
    BUILDERS = {"diaktoros/run_supervisor/turn_prompts.py::isolated_prompt",
                "diaktoros/run_supervisor/turn_prompts.py::conflict_prompt",
                "diaktoros/run_supervisor/turn_prompts.py::ci_fix_prompt",
                "diaktoros/run_supervisor/turn_prompts.py::issue_fix_prompt",
                "diaktoros/run_supervisor/turn_prompts.py::triage_prompt"}

    def test_every_builder_in_the_package_is_listed_and_calls_the_shared_reader(self):
        found = scan()
        self.assertEqual(set(found), self.BUILDERS)
        self.assertEqual([name for name, ok in found.items() if not ok], [])

    def test_a_builder_that_skips_the_reader_fails_the_scan(self):
        # The negative case: a builder anywhere, with or without the reader.
        skips = ("from .. import prompts\n"
                 "def new_prompt(loop, row):\n"
                 "    return prompts.render_isolated('x', pr=row['pr'])\n")
        self.assertEqual(builders(skips, "diaktoros/other.py"),
                         {"diaktoros/other.py::new_prompt": False})
        calls = skips.replace("row['pr'])", "row['pr']) + issue_facts.section(loop, 1)")
        self.assertEqual(builders(calls, "diaktoros/other.py"),
                         {"diaktoros/other.py::new_prompt": True})
        # The triage builder as it stood before #515 review F2 fails it.
        src = inspect.getsource(turn_prompts.triage_prompt).replace(
            "issue_facts.comments_section", "len")
        self.assertFalse(builders(src, "t")["t::triage_prompt"])


if __name__ == "__main__":
    unittest.main()
