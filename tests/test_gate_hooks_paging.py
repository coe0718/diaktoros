"""#558: gate.hooks_read must read every hook page; a full first page is not the whole list."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diaktoros import doctor, gate, gh  # noqa: E402

LOOP = {"id": "one", "repo": "owner/one",
        "seats": {"reviewer": {"route": "one-review"}, "fixer": {"route": "one-fix"}}}
URLS = {"one-review": "https://h.example/webhooks/one-review",
        "one-fix": "https://h.example/webhooks/one-fix"}


def hook(url, active=True):
    return {"active": active, "config": {"url": url}}


def filler(n):
    return [hook(f"https://h.example/webhooks/other-{i}") for i in range(n)]


class GateHooksPaging(unittest.TestCase):
    def read(self, pages):
        def fetch(loop, path, *a, **k):
            page = int(path.split("&page=")[1]) if "&page=" in path else 1
            if page > len(pages):
                return None, "HTTP 500"
            return pages[page - 1], ""
        with mock.patch.object(gh, "fetch", side_effect=fetch), \
                mock.patch.object(doctor, "seat_hook_url", lambda loop, route: URLS[route]):
            return gate.hooks_read(LOOP)

    def test_seat_hooks_on_a_later_page_are_armed(self):
        pages = [filler(100), [hook(URLS["one-review"]), hook(URLS["one-fix"])]]
        self.assertEqual(self.read(pages), (True, ""))

    def test_a_failing_later_page_is_unknown_not_unarmed(self):
        armed, error = self.read([filler(100)])
        self.assertIsNone(armed)
        self.assertIn("page 2", error)

    def test_a_short_page_without_the_hooks_is_still_unarmed(self):
        self.assertEqual(self.read([filler(3)]), (False, "reviewer, fixer"))


if __name__ == "__main__":
    unittest.main()
