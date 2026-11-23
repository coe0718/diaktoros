"""#2538: the docs describe the Desktop health fallback's status matcher as it is.

``retryFailure`` (desktop/plugin.js) reads a status from an error body that starts with ``{`` and
ends with ``}``, whether or not it parses as JSON, so ``503: {not json}`` is an HTTP 503 and
``503: []`` is a transport failure (tests/desktop_behavior.mjs pins both). The docs said JSON.
"""
import _home_guard  # noqa: F401
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ('docs/desktop-read-diagnostics.md', 'docs/operations.md', 'docs/security.md')


class HealthStatusMatcherDocs(unittest.TestCase):
    def test_the_docs_say_brace_delimited_not_json(self):
        for name in DOCS:
            text = ' '.join((ROOT / name).read_text().split())
            with self.subTest(doc=name):
                self.assertIn('brace-delimited', text)
                self.assertIsNone(re.search(r'body is not JSON|with a JSON body|JSON error body|'
                                            r'without a JSON body|<status>: <JSON body>', text))

    def test_the_page_matcher_is_the_brace_shape(self):
        page = (ROOT / 'desktop/plugin.js').read_text()
        self.assertIn(r"/(?:^|\s)(\d{3}):\s*(\{[\s\S]*\})\s*$/", page)


if __name__ == '__main__':
    unittest.main()
