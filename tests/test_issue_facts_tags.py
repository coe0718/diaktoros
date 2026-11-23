"""Twenty-first to twenty-fourth reviews on #2573, beside ``tests/test_issue_facts_fences.py``:
an HTML tag's attributes are never shown text, whether the tag is inline in a Markdown item or
split across lines in an HTML list item, so a status in one marks nothing. GitHub is mocked."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest

from test_issue_facts_fences import Mocked, check


class Tags(Mocked):
    def test_an_inline_tags_attribute_marks_nothing(self):
        # Twenty-first: in a Markdown item an inline tag's attribute is not shown either, so a
        # `met` in one marks nothing and a `not met` in one unmarks nothing; its text still counts.
        self.assertIn("has no status", check('- second: <span title="met">export flag</span>\n'))
        for body in ('- second: met <span title="not met">export flag</span>\n',
                     '- second: <span title="x">export flag</span> met\n',
                     '- second: <b>met</b> with `<span title="x">` as evidence\n'):
            with self.subTest(body):
                self.assertEqual(check(body), "")

    def test_a_tag_split_across_lines_stays_in_its_html_item(self):
        # Twenty-second: a tag split over two lines in an HTML item is read whole, so the item
        # keeps its text and its attribute marks nothing.
        self.assertIn('"- second: export flag" has no status',
                      check('<ul>\n<li><span\n title="met">second: export flag</span></li>\n</ul>\n'))
        self.assertIn('"- second: export flag" has no status',
                      check('<ul>\n<li><span title="a\nmet">second: export flag</span></li>\n</ul>\n'))
        self.assertEqual(check('<ul>\n<li><span\n title="x">second: met</span></li>\n</ul>\n'), "")

    def test_a_tag_split_at_an_attributes_equals_sign_stays_in_its_html_item(self):
        # Round 23: a split between an attribute's `=` and its value, or between its name and
        # `=`, is read whole too; a marked item split the same way still passes.
        for split in ('title=\n"met"', 'title =\n  "met"', 'title\n="met"'):
            with self.subTest(split):
                self.assertIn('"- second: export flag" has no status',
                              check(f'<ul>\n<li><span {split}>second: export flag</span></li>\n</ul>\n'))
                self.assertEqual(check(f'<ul>\n<li><span {split}>second: met</span></li>\n</ul>\n'), "")

    def test_a_tag_left_open_in_a_quoted_value_hides_the_tags_in_it(self):
        # Round 24: an apparent `<li>` inside a quoted value still open at the line's end is part
        # of that value, never an item of its own; a marked item split the same way passes.
        self.assertIn('"- second: export flag" has no status',
                      check('<ul>\n<li><span title="<li>\nmet">second: export flag</span></li>\n</ul>\n'))
        self.assertEqual(check('<ul>\n<li><span title="<li>\nx">second: met</span></li>\n</ul>\n'), "")


if __name__ == "__main__":
    unittest.main()
