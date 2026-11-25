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
import re
import struct
import unittest
import urllib.parse

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


def srcset_urls(srcset: str) -> list[str]:
    """The URL of each ``srcset`` candidate, as the HTML spec splits them: a URL is a run of
    non-space characters (a ``data:`` URL keeps its commas), then any descriptors up to a comma."""
    urls, pos = [], 0
    while (m := re.compile(r"[\s,]*(\S+)").match(srcset, pos)) and m.group(1):
        url, pos = m.group(1), m.end()
        if url.endswith(","):
            url = url.rstrip(",")
        else:                                   # skip this candidate's descriptors
            comma = srcset.find(",", pos)
            pos = len(srcset) if comma < 0 else comma + 1
        urls.append(url)
    return urls


class ImageParser(HTMLParser):
    """Every image path an HTML page or README loads: each ``<img src>`` and each ``srcset``
    candidate on an ``<img>`` or a ``<source>``."""

    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img" and a.get("src"):
            self.paths.append(a["src"])
        if tag in ("img", "source") and a.get("srcset"):     # a browser may pick either
            self.paths += srcset_urls(a["srcset"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def broken_images(text: str, root: pathlib.Path) -> list[str]:
    """Each image ``text`` loads that is not a file inside ``root`` (#2250). README sits at the
    root and the page is published there with docs/ beside it, so a relative path on either is a
    path from the repository root. data: URIs load nothing; a remote, absolute or escaping path
    is not a repository file, so it is broken too. No image at all is reported as well."""
    parser = ImageParser()
    parser.feed(text)
    found, broken = 0, []
    for src in parser.paths:
        url = urllib.parse.urlsplit(src)
        if url.scheme == "data":
            continue
        found += 1
        path = (root / urllib.parse.unquote(url.path)).resolve()
        if (url.scheme or url.netloc or url.path.startswith("/") or url.query or url.fragment
                or not path.is_relative_to(root.resolve()) or not path.is_file()):
            broken.append(src)
    return broken if found else ["no image found: the scan reads nothing"]


class EveryImageResolves(unittest.TestCase):
    def test_each_image_in_the_readme_and_the_site_is_a_repository_file(self):
        # #2250: only the hero <picture> was checked, so a moved README logo shipped broken.
        for page in (ROOT / "README.md", PAGE):
            with self.subTest(page.name):
                self.assertEqual(broken_images(page.read_text(encoding="utf-8"), ROOT), [])

    def test_the_guard_fails_on_a_broken_readme_image_and_passes_a_good_one(self):
        # #2250: drive the page guard itself on a copy of the repository's two pages.
        import sys
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "good.png").write_bytes(b"x")
            (root / "index.html").write_text('<img src="good.png">')
            for readme, broken in (('<img src="good.png">', 0), ('<img src="moved.png">', 1),
                                   ('<img src="good.png" srcset="good.png 1x, missing.png 2x">', 1)):
                with self.subTest(readme):
                    (root / "README.md").write_text(readme)
                    result = unittest.TestResult()
                    module = sys.modules[__name__]
                    with mock.patch.object(module, "ROOT", root), \
                            mock.patch.object(module, "PAGE", root / "index.html"):
                        EveryImageResolves("test_each_image_in_the_readme_and_the_site_is_a_repository_file").run(result)
                    self.assertEqual(len(result.failures + result.errors), broken)

    def test_the_scan_reports_each_unservable_image_and_passes_a_good_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "repo"
            (root / "img").mkdir(parents=True)
            (root / "img/good.png").write_bytes(b"x")
            (root.parent / "outside.png").write_bytes(b"x")
            ok = '<img src="img/good.png"><source srcset="data:image/png;base64,AAAA">'
            self.assertEqual(broken_images(ok, root), [])
            self.assertEqual(broken_images('<source srcset="img/good.png 1x, img/good.png 2x">', root), [])
            for bad in ("img/missing.png", "/etc/passwd", "../outside.png", "https://x.test/a.png",
                        "img/good.png?v=1"):
                with self.subTest(bad):
                    self.assertEqual(broken_images(f'{ok}<img src="{bad}">', root), [bad])
            srcset = '<source srcset="img/good.png 1x,img/missing.png 2x">'
            self.assertEqual(broken_images(srcset, root), ["img/missing.png"])
            self.assertEqual(broken_images('<img src="data:,x">', root), ["no image found: the scan reads nothing"])


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
