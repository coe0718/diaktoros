"""Seventh review on #2573 (F7): fenced code in a Requirements section is code, as Markdown
renders it, not requirement items, statuses or headings. A ``diff`` given as evidence read as
unmarked items, so a fully marked APPROVE was refused. GitHub is mocked."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

import test_issue_facts as tif
from diaktoros import gh, issue_facts

LOOP = tif.LOOP
DIFF = "Example evidence:\n```diff\n- old value\n+ new value\n```\n"


def check(body: str, verdict: str = "APPROVE") -> str:
    return issue_facts.check(LOOP, 7, verdict, "## Requirements\n- first: met\n\n" + body)


class Mocked(unittest.TestCase):
    def setUp(self):
        fake = tif.Fake()
        for name, fn in (("api", fake.api), ("issue_comments_read", fake.comments)):
            patch = mock.patch.object(gh, name, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)


class Fences(Mocked):
    def test_fenced_evidence_is_not_an_item(self):
        for body in (DIFF, DIFF.replace("```", "~~~~"), "  ```\n  - nested: code\n  ```\n",
                     "````md\n- a: x\n```\n- b: y\n````\n"):       # a shorter fence inside
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_a_real_unmarked_item_after_the_fence_is_still_refused(self):
        for fence in (DIFF, "```sh\n# a shell comment, not a heading\n```\n"):
            with self.subTest(fence):
                reason = check(fence + "- second: export flag\n")
                self.assertIn('"- second: export flag" has no status', reason)

    def test_a_status_inside_a_fence_counts_for_nothing(self):
        self.assertIn("`not met` requirement", check("- two: not met\n"))       # control
        self.assertEqual(check("```\n- two: not met\n```\n"), "")
        reason = issue_facts.check(LOOP, 7, "APPROVE", "## Requirements\n```\n- one: met\n```\n")
        self.assertIn("giving each requirement `met`", reason)     # an empty section

    def test_an_unclosed_fence_runs_to_the_end(self):
        self.assertEqual(check("```diff\n- old value\n- not met\n"), "")


class IndentedFences(Mocked):
    """Eighth review on #2573 (F8): a fence opens only up to three columns past the content column
    of the list item it is in; more is indented code. And a block in a list item ends with the
    item, so a real top-level item after it is read and, unmarked, refused."""

    def test_an_indented_backtick_line_hides_no_real_item(self):
        for body in ("    ```\n\n- second: export flag\n",              # Vex's case: in item one
                     "Prose.\n\n    ```\n\n- second: export flag\n",   # after prose: code
                     "  ```diff\n  - old\n\n- second: export flag\n"):  # unclosed, in item one
            with self.subTest(body):
                self.assertIn('"- second: export flag" has no status', check(body))

    def test_a_fence_in_a_list_item_is_still_code(self):
        for body in ("- two: met\n\n     ```\n     - nested: code\n     ```\n",
                     "1. two: met\n\n   ```diff\n   - old value\n   ```\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")


class IndentedCode(Mocked):
    """Ninth review on #2573 (F9): a list marker four or more columns past its container's
    content column (0 outside a list) is indented code, as Markdown renders it, not an item."""

    def test_indented_code_evidence_is_not_an_item(self):
        for body in ("Example code:\n\n    - command --help\n",        # Vex's case
                     "Example code:\n\n\t1. step one\n",
                     "- two: met\n\n  Code:\n\n      - nested --flag\n"):  # in an item
            with self.subTest(body):
                self.assertEqual(check(body), "")
        self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", "## Requirements\nCode:\n\n"
                                           "    - x --y\n\n- one: met\n"), "")

    def test_a_real_unmarked_item_after_indented_code_is_still_refused(self):
        reason = check("Example code:\n\n    - command --help\n\n- second: export flag\n")
        self.assertIn('"- second: export flag" has no status', reason)


class HtmlComments(Mocked):
    """Tenth review on #2573 (F10): an HTML comment is hidden, as Markdown renders it, so a list
    item commented out is not a requirement."""

    def test_a_commented_out_item_is_not_an_item(self):
        for body in ("<!--\n- draft requirement removed\n-->\n",           # Vex's case
                     "<!-- - draft one: x -->\n",
                     "- two: met\n\n  <!--\n  - nested draft\n  -->\n",
                     "<!-- a note\n\n- draft after a blank line\n\n-->\n- three: met\n",
                     "- four: met\n  <!-- draft\n  - nested draft\n\n  -->\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_a_real_unmarked_item_outside_the_comment_is_still_refused(self):
        for body in ("<!--\n- draft requirement removed\n-->\n- second: export flag\n",
                     "- second: export flag <!-- inline, not a block -->\n",
                     # Eleventh review (F11): a comment left open in an item ends with it.
                     "- first: met\n  <!-- draft\n- second: export flag\n",
                     "- first: met\n\n  <!-- draft\n\n- second: export flag\n"):
            with self.subTest(body):
                self.assertIn('"- second: export flag', check(body))

    def test_a_status_inside_an_inline_comment_marks_nothing(self):
        # Twelfth review (F12): ``met`` hidden in a comment marked the item it sat in.
        for body in ("- second: export flag <!-- met -->\n",                 # Vex's case
                     "- second: export flag <!--\n  met -->\n",              # over two lines
                     "- second: export flag <!-- a --> b <!-- met -->\n",
                     "- second: export flag <!-- met\n"):                    # never closed: text
            with self.subTest(body):
                self.assertIn('"- second: export flag', check(body))

    def test_a_visible_status_beside_an_inline_comment_still_counts(self):
        for body in ("- second: met <!-- not met -->\n",
                     "- second: <!-- draft --> met\n",
                     "- second: met <!-- open\n- third: met\n"):  # a new item ends the comment
            with self.subTest(body):
                self.assertEqual(check(body), "")
        reason = check("- second: met <!-- open\n- third: export flag\n")
        self.assertIn('"- third: export flag" has no status', reason)  # shown, not hidden

    def test_a_comment_opener_inside_a_code_span_hides_nothing(self):
        # Thirteenth review (F13): `<!--` in a code span hid the `not met` after it.
        self.assertIn("`not met`", check("- second: met `<!--` not met\n"))
        self.assertIn("`not met`", check("- second: met ``a ` <!--`` not met\n"))
        self.assertIn('"- second: x `', check("- second: x ` <!-- met -->\n"))  # no span: hidden
        self.assertEqual(check("- second: `<!--` x <!-- not met --> met\n"), "")

    def test_a_code_span_across_lines_hides_nothing(self):
        # Fourteenth review (F14): a span that closes on the next line was read as a comment.
        self.assertIn("`not met`", check("- second: met `example\n  text <!--` not met\n"))
        reason = check("- second: `a <!--` x\n  y ` <!-- met -->\n")   # the span closes early
        self.assertIn('"- second: `a <!--` x" has no status', reason)
        self.assertEqual(check("- second: met `open\n\n- third: x <!-- not met --> met\n"), "")

    def test_a_marker_that_cannot_interrupt_the_text_is_the_text(self):
        # Second review of F14: ``2.`` cannot start a list inside a paragraph, so it is the
        # item's text, and the code span runs on through it.
        self.assertIn("`not met`", check("- second: met `example\n  2. text <!--` not met\n"))
        self.assertEqual(check("- second: met\n2. more of its text\n"), "")      # one item
        self.assertIn('"2. third: export flag" has no status',                       # a sibling
                      check("1. second: met\n2. third: export flag\n"))
        self.assertIn('"- third: export flag" has no status',                        # a bullet
                      check("- second: met `x\n- third: export flag <!--` met\n"))

    def test_a_line_that_only_looks_like_a_fence_is_the_text(self):
        # Sixteenth review (F16): a backtick fence with a backtick after it is no fence, so the
        # code span runs on through it; a real fence still ends the paragraph and the span.
        self.assertIn("`not met`", check("- second: met `example\n  ``` <!--` not met\n"))
        self.assertIn("`not met`", check("- second: met `example\n        ~~~ <!--` not met\n"))
        self.assertIn('"- second: `example" has no status',                     # a real one
                      check("- second: `example\n  ```\n  met`\n  ```\n"))

    def test_an_escaped_backtick_opens_no_code_span(self):
        # Fifteenth review (F15): ``\\``` read as a span opener, so a real comment showed.
        self.assertIn('"- second: export flag', check("- second: export flag \\`x <!-- met --> y`\n"))
        self.assertIn("`not met`", check("- second: met \\\\`<!--` not met\n"))  # \\ is a \
        self.assertEqual(check("- second: x \\<!-- met -->\n"), "")  # shown as text: met
        self.assertEqual(check("- second: export flag `x <!-- met --> y` met\n"), "")


class HtmlBlocks(Mocked):
    """Seventeenth review on #2573 (F17): an HTML block other than a comment is raw HTML, as
    Markdown renders it, so a line in ``<pre>`` evidence that begins ``- `` is not an item."""

    def test_html_block_evidence_is_not_an_item(self):
        for body in ("<pre>\n- command --help\n</pre>\n",                   # Vex's case
                     "<PRE class=x>\n\n- command --help\n</pre>\n",          # to its </pre>
                     "<div>\n- command --help\n</div>\n", "<?php\n- x\n?>\n",
                     "<span>\n- command --help\n",                         # a lone tag
                     "- two: met\n\n  <pre>\n  - nested --flag\n  </pre>\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_a_real_unmarked_item_after_the_block_is_still_refused(self):
        for body in ("<pre>\n- command --help\n</pre>\n- second: export flag\n",
                     "<div>\n- command --help\n\n- second: export flag\n",   # a blank ends it
                     "Prose.\n<span>\n- second: export flag\n",   # a lone tag: the prose's text
                     "    <pre>\n\n- second: export flag\n"):     # indented: code, no block
            with self.subTest(body):
                self.assertIn('"- second: export flag" has no status', check(body))

    def test_a_status_in_shown_html_still_counts(self):
        # Eighteenth review (F18): a <ul> or <div> block is shown text, so its `not met` counts
        # (even after a heading-like line in it); code or hidden HTML's does not.
        for body in ("<ul>\n<li>second: not met</li>\n</ul>\n",                 # Vex's case
                     "<div>\n- second: not met\n</div>\n",
                     "<div>\n# Notes\n**Notes**\n<li>second: not met</li>\n</div>\n"):
            with self.subTest(body):
                self.assertIn("`not met`", check(body))
        for body in ("<pre>\n- second: not met\n</pre>\n",
                     "<ul>\n<li>second: met <!-- not met --></li>\n</ul>\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")


    def test_a_shown_html_list_item_is_a_requirement(self):
        # Nineteenth review (F19): a shown <li> outside any Markdown item is an item, so an
        # unmarked one is refused on APPROVE, as `- second: export flag` is.
        for body in ("<ul>\n<li>second: export flag</li>\n</ul>\n",            # Vex's case
                     "<ul><li>second: met</li><li>third: export flag</li></ul>\n",
                     "<OL>\n  <li class=x>\n    third: export flag\n  </li>\n</OL>\n"):
            with self.subTest(body):
                self.assertIn("export flag", check(body))
                self.assertIn("has no status", check(body))
                self.assertEqual(check(body, "REQUEST_CHANGES"), "")
        for body in ("<ul>\n<li>second: met</li>\n<li>third: deferred to #9</li>\n</ul>\n",
                     "<ul>\n<li>second: met<ul><li>evidence: a.py:3</li></ul></li>\n</ul>\n",
                     "- two: met\n\n  <ul>\n  <li>evidence: a.py:3</li>\n  </ul>\n",
                     "<pre>\n<li>evidence: a.py:3</li>\n</pre>\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")


if __name__ == "__main__":
    unittest.main()
