"""Regression checks for the site's social cards and featured case-study teaser.

These checks deliberately read the shipped HTML/CSS without Node or a browser,
so removing the features makes the tests fail even on lightweight CI runners.
"""
import _home_guard  # noqa: F401  # first import: temp HOME/HERMES_HOME
from html.parser import HTMLParser
import pathlib
import re
import unittest
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "site" / "index.html"
BANNER = ROOT / "docs" / "banner.jpg"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = []
        self.ids = set()
        self.feature_count = 0
        self.feature_links = []
        self._in_feature = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.meta.append(a)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "aside" and "proof-feature" in a.get("class", "").split():
            self.feature_count += 1
            self._in_feature += 1
        elif self._in_feature:
            self._in_feature += 1
        if tag == "a" and self._in_feature:
            self.feature_links.append(a.get("href"))

    def handle_endtag(self, tag):
        if self._in_feature:
            self._in_feature -= 1


class SiteSocialPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.page = PageParser()
        cls.page.feed(cls.html)

    def meta(self, key, value):
        matches = [m.get("content") for m in self.page.meta
                   if m.get(key) == value]
        self.assertEqual(len(matches), 1, f"Expected exactly one {value} tag")
        return matches[0]

    def test_social_cards_use_a_real_published_banner(self):
        og_image = self.meta("property", "og:image")
        x_image = self.meta("name", "twitter:image")
        self.assertEqual(og_image, x_image)
        self.assertEqual(self.meta("name", "twitter:card"), "summary_large_image")
        self.assertIn("Diaktoros", self.meta("property", "og:image:alt"))
        self.assertIn("Diaktoros", self.meta("name", "twitter:image:alt"))
        parsed = urlparse(og_image)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "coe0718.github.io")
        self.assertEqual(parsed.path, "/diaktoros/docs/banner.jpg")
        self.assertTrue(BANNER.is_file(), "Social preview asset must exist")
        self.assertEqual(BANNER.read_bytes()[:3], b"\\xff\\xd8\\xff")

    def test_featured_teaser_links_to_existing_case_study(self):
        self.assertEqual(self.page.feature_count, 1)
        self.assertIn("case-study", self.page.ids)
        self.assertEqual(self.page.feature_links, ["#case-study"])
        self.assertIn("Three reviews. Two fixes. One adjudication.", self.html)
        self.assertIn("Explore PR #1624", self.html)

    def test_teaser_has_mobile_stack_treatment(self):
        # An inexpensive structural guard; actual visual layout still needs
        # a browser smoke test. Require the mobile breakpoint to stack it.
        self.assertRegex(
            self.html,
            r"@media\\s*\\(max-width:\\s*600px\\)\\s*\\{"
            r"[\\s\\S]*?\\.proof-feature\\s*\\{"
            r"[^}]*flex-direction:\\s*column",
        )
        self.assertRegex(self.html, r"\\.proof-feature\\s*\\{[^}]*display:\\s*flex")


if __name__ == "__main__":
    unittest.main()
