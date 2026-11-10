"""The site hero shows the logo with no black box, in a variant for each colour scheme.

The hero once used the logo on its own black background, which showed as a dark rounded
rectangle on the navy page. These checks read the shipped HTML and the image headers without
a browser: removing the <picture>, pointing it at a missing file, or shipping a variant without
an alpha channel makes them fail. Where Pillow is installed, one more check decodes both
variants and requires most of each to be fully transparent, which the boxed logo was not.
"""
import _home_guard  # noqa: F401  # first import: temp HOME/HERMES_HOME
from html.parser import HTMLParser
import pathlib
import struct
import unittest

try:  # Pillow is optional here, as node is for the viewer tests: the pixel check needs it.
    from PIL import Image
except ImportError:  # pragma: no cover - depends on the runner
    Image = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "site" / "index.html"
DARK = "(prefers-color-scheme: dark)"


class HeroParser(HTMLParser):
    """Collects the <source> and <img> tags inside the hero's <picture class="mast-logo-wrap">."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.sources = []
        self.imgs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "picture" and "mast-logo-wrap" in a.get("class", "").split():
            self.depth += 1
        elif self.depth and tag == "source":
            self.sources.append(a)
        elif self.depth and tag == "img":
            self.imgs.append(a)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "picture" and self.depth:
            self.depth -= 1


def webp_header(path: pathlib.Path) -> tuple[int, int, bool]:
    """Width, height and whether an alpha channel is present, from a VP8X WebP's chunks."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or data[12:16] != b"VP8X":
        raise AssertionError(f"{path.name} is not an extended (VP8X) WebP")
    flags = data[20]
    width = struct.unpack("<I", data[24:27] + b"\0")[0] + 1
    height = struct.unpack("<I", data[27:30] + b"\0")[0] + 1
    # Lossy WebP keeps its alpha in an ALPH chunk; lossless keeps it inside VP8L.
    has_alpha = bool(flags & 0x10) and (b"ALPH" in data or b"VP8L" in data)
    return width, height, has_alpha


class SiteHeroLogoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.hero = HeroParser()
        cls.hero.feed(cls.html)

    def asset(self, src: str) -> pathlib.Path:
        # index.html is published at the site root and docs/ beside it, so a relative
        # "docs/assets/x" on the page is the repository's docs/assets/x.
        self.assertTrue(src.startswith("docs/assets/"), src)
        path = ROOT / src
        self.assertTrue(path.is_file(), f"{src} is missing")
        return path

    def test_hero_has_a_dark_source_and_a_light_fallback(self):
        self.assertEqual(len(self.hero.imgs), 1, "expected one <img> in the hero <picture>")
        dark = [s for s in self.hero.sources if s.get("media") == DARK]
        self.assertEqual(len(dark), 1, "expected one prefers-color-scheme: dark <source>")
        self.assertNotEqual(dark[0]["srcset"], self.hero.imgs[0]["src"])
        self.assertEqual(self.hero.imgs[0].get("alt"), "Diaktoros")

    def test_the_switch_matches_the_pages_own_dark_colours(self):
        self.assertIn(f"@media {DARK}", self.html)

    def test_both_variants_exist_with_real_alpha_and_match_the_size_hints(self):
        img = self.hero.imgs[0]
        hint = (int(img["width"]), int(img["height"]))
        dark = next(s for s in self.hero.sources if s.get("media") == DARK)
        for src in (dark["srcset"], img["src"]):
            width, height, has_alpha = webp_header(self.asset(src))
            self.assertTrue(has_alpha, f"{src} has no alpha channel, so it would show a box")
            self.assertEqual((width, height), hint, f"{src} does not match the <img> size hints")

    @unittest.skipUnless(Image, "Pillow unavailable")
    def test_both_variants_are_mostly_see_through_not_a_box(self):
        img = self.hero.imgs[0]
        dark = next(s for s in self.hero.sources if s.get("media") == DARK)
        for src in (dark["srcset"], img["src"]):
            with Image.open(self.asset(src)) as im:
                alpha = im.convert("RGBA").getchannel("A")
                clear = alpha.histogram()[0] / (im.width * im.height)
                corners = [alpha.getpixel(p) for p in
                           ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1))]
            # The boxed logo was over 99% opaque; the knocked-out artwork leaves most pixels clear.
            self.assertGreater(clear, 0.5, f"{src} is only {clear:.0%} transparent: a box would show")
            self.assertEqual(corners, [0, 0, 0, 0], f"{src} has opaque corners")

    def test_logo_spans_the_hero_and_is_centred(self):
        self.assertRegex(self.html, r"\.mast-logo-wrap\s*\{[^}]*grid-column:\s*1\s*/\s*-1")
        self.assertRegex(self.html, r"\.mast-logo\s*\{[^}]*margin:\s*0 auto")


if __name__ == "__main__":
    unittest.main()
