"""#2636: raw HTML in a Requirements section is refused on an APPROVE, never parsed.

Reviews F17 to F22 on #2573 each found one more HTML construct a hand-written reader got wrong
(``<pre>`` evidence, ``<ul>`` items, comments around tags, attributes, tags split across lines,
quoted values holding tags). Every one of those inputs is now refused with one reason; Markdown
items, fenced or code-span HTML evidence and comments still pass. GitHub is mocked."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest

from test_issue_facts_fences import Mocked, check

RAW = "holds raw HTML"
# The inputs reviews F17-F22 and rounds 23-24 on #2573 raised, marked and unmarked alike.
REVIEWED = (
    "<pre>\n- command --help\n</pre>\n", "<PRE class=x>\n\n- command --help\n</pre>\n",
    "<div>\n- command --help\n</div>\n", "<?php\n- x\n?>\n", "<span>\n- command --help\n",
    "- two: met\n\n  <pre>\n  - nested --flag\n  </pre>\n",
    "<pre>\n- command --help\n</pre>\n- second: export flag\n",
    "<div>\n- command --help\n\n- second: export flag\n", "Prose.\n<span>\n- second: met\n",
    "<ul>\n<li>second: not met</li>\n</ul>\n", "<div>\n# Notes\n<li>second: met</li>\n</div>\n",
    "<ul>\n<li>second: export flag</li>\n</ul>\n", "<ul><li>second: met</li></ul>\n",
    "<OL>\n  <li class=x>\n    third: met\n  </li>\n</OL>\n",
    "- two: met\n\n  <ul>\n  <li>evidence: a.py:3</li>\n  </ul>\n",
    "<ul>\n<!-- <ul> -->\n<li>second: met</li>\n</ul>\n",
    '<ul>\n<li><span title="x">second: met</span></li>\n</ul>\n',
    '- second: <span title="met">export flag</span>\n', '- second: <b>met</b>\n',
    '<ul>\n<li><span\n title="met">second: export flag</span></li>\n</ul>\n',
    '- second: <span\n  title="met">export flag</span>\n',
    '<ul>\n<li><span title=\n"met">second: export flag</span></li>\n</ul>\n',
    '<ul>\n<li><span title="<li>\nmet">second: export flag</span></li>\n</ul>\n')


class RawHtml(Mocked):
    def test_every_reviewed_html_case_is_refused_on_an_approve(self):
        for body in REVIEWED:
            with self.subTest(body):
                self.assertIn(RAW, check(body))

    def test_markdown_code_and_comments_still_pass(self):
        # Controls: HTML as evidence in a code span or fenced block, and a comment, are not raw
        # HTML; a status beside them counts.
        for body in ('- second: met with `<span title="x">` as evidence\n',
                     "- second: met\n\n  ```html\n  <ul><li>third</li></ul>\n  ```\n",
                     "- second: met <!-- <li>draft</li> -->\n", "<!-- <ul><li>x</li></ul> -->\n",
                     "- second: met, `a < b` holds\n"):
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_html_after_a_comment_on_its_line_is_refused(self):
        # #2642 F1: CommonMark keeps a comment block's whole closing line in the block, so HTML
        # after the ``-->`` is raw HTML; another comment there is not.
        for body in ("<!-- note --><ul><li>second: export flag</li></ul>\n",
                     "<!-- note --> <b>second: met</b>\n", "<!--\nnote --><ul><li>x</li></ul>\n",
                     "  <!-- a --> b <!-- c --><i>met</i>\n"):
            with self.subTest(body):
                self.assertIn(RAW, check(body))
        self.assertEqual(check("<!-- a --> <!-- b -->\n<!--\nc -->  \n"), "")

    def test_inline_instructions_declarations_and_cdata_are_raw(self):
        # #2642 F4: CommonMark's other inline raw HTML holds no status either, even across lines.
        for html in ("<?php met ?>", "<!X met>", "<![CDATA[met]]>", "<?x\nmet ?>"):
            with self.subTest(html):
                self.assertIn(RAW, check(f"- second: {html}\n"))
                self.assertIn(RAW, check(f"- second: export flag {html}\n"))

    def test_an_unmarked_markdown_item_beside_a_comment_is_still_refused(self):
        self.assertIn('"- second: export flag" has no status',
                      check("<!-- note -->\n- second: export flag\n"))


if __name__ == "__main__":
    unittest.main()
