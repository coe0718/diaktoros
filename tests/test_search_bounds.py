"""Searches that stop short say so (#2298, #2357, #1868, #1870): a bounded or failed search is
never reported as a complete one, and a complete one is not reported as cut short."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_deliveries as apps  # noqa: E402
from diaktoros import app_deliveries, gh, hook_ping, selftest, trace  # noqa: E402
from diaktoros.doctor import hook_checks  # noqa: E402

LOOP = {"id": "widgets", "repo": "acme/widgets", "read_token": "reader",
        "seats": {"reviewer": {"route": "widgets-review"}, "fixer": {"route": "widgets-fix"}},
        "adjudicator": {}}
SINCE = "2026-11-02T00:00:00Z"


def item(n: int, created: str = "2026-11-04T00:00:00Z") -> dict:
    return {"number": n, "created_at": created}


def full(start: int, created: str = "2026-11-04T00:00:00Z") -> list[dict]:
    return [item(start + n, created) for n in range(gh.REVIEW_PAGE_SIZE)]


class AppLogWindow(apps.Base):
    """#2298: the App's log is searched to its newest 300; the lines name that window."""

    def busy_log(self, *ours):
        # 300 deliveries of other repositories first, then this repository's past the window.
        return [apps.delivery(n, repo=apps.OTHER_ID) for n in range(400, 100, -1)] + list(ours)

    def test_doctor_names_the_window_instead_of_claiming_none_yet(self):
        self.app(self.busy_log(apps.delivery(5)))
        found = hook_checks.check_app_deliveries(apps.LOOP)
        self.assertTrue(found.startswith("no deliveries for this repository"), found)
        self.assertIn("newest 300", found)
        self.assertNotIn("yet", found)

    def test_selftest_warns_with_the_window(self):
        self.app(self.busy_log(apps.delivery(5)))
        report = selftest.Report(out=io.StringIO())
        with mock.patch.object(selftest.gh, "hooks_read"):
            selftest.check_hook_signatures(report, apps.LOOP)
        (_, _, mark, detail, fix), = [tuple(r[:5]) for r in report.results]
        self.assertEqual(mark, selftest.WARN, detail)
        self.assertIn("newest 300", detail)
        self.assertIn("newest 300", fix)

    def test_ping_names_the_window_it_searched(self):
        fake = self.app(self.busy_log(apps.delivery(6, "ping", None)))
        with mock.patch.object(hook_ping, "PING_WAIT", 0.0):
            status, line = app_deliveries.ping(apps.LOOP)
        self.assertEqual(status, hook_ping.SILENT, line)
        self.assertIn("newest 300 deliveries", line)
        self.assertFalse([p for m, p, _ in fake.calls if m == "POST"])

    def test_a_delivery_inside_the_window_is_still_found(self):
        # Guard: the window's wording changes nothing for a delivery inside it.
        self.app([apps.delivery(n, repo=apps.OTHER_ID) for n in range(50)] + [apps.delivery(5)])
        self.assertEqual(hook_checks.check_app_deliveries(apps.LOOP), "latest delivery 200")


class TraceListingFailure(unittest.TestCase):
    """#2357: a failed delivery listing is an unfinished search, not a negative one."""

    hooks = [{"id": 9, "config": {"url": "https://gw.example/webhooks/widgets-review"}}]

    def fetch(self, api):
        with mock.patch.object(gh, "hooks_read", return_value=(self.hooks, "")), \
                mock.patch.object(gh, "api", side_effect=api) as calls, \
                mock.patch.object(gh, "last_error", return_value="HTTP 502"), \
                self.assertRaises(trace.TraceError) as caught:
            trace.fetch_delivery(LOOP, "wanted-guid", "admin")
        return str(caught.exception), calls.call_count

    def test_a_failed_listing_is_not_reported_as_searched(self):
        message, calls = self.fetch(lambda *args, **kwargs: None)
        self.assertEqual(calls, 1)
        self.assertNotIn("were searched", message)
        self.assertIn("could not be read", message)
        self.assertIn("hook 9 page 1: HTTP 502", message)

    def test_a_failure_on_a_later_page_is_also_unfinished(self):
        pages = iter([[{"id": n, "guid": f"g{n}"} for n in range(100)], None])
        message, calls = self.fetch(lambda *args, **kwargs: next(pages))
        self.assertEqual(calls, 2)
        self.assertIn("hook 9 page 2: HTTP 502", message)

    def test_an_empty_listing_is_still_a_complete_search(self):
        # Guard: GitHub's own end of the list is an answer, and the refusal says what was searched.
        message, _ = self.fetch(lambda *args, **kwargs: [])
        self.assertIn("the last 300 per hook were searched", message)


class OldProbe(unittest.TestCase):
    """#1868: a probe page whose first (newest) item is older than the floor proves the listing
    whole: the listing is sorted by created, descending."""

    def read(self, probe):
        with mock.patch.object(gh, "MAX_ISSUE_PAGES", 2), \
                mock.patch.object(gh, "fetch", side_effect=[(full(0), ""), (full(100), ""),
                                                            probe]) as fetch:
            got = gh.open_issues_read(LOOP, SINCE)
        self.assertEqual(fetch.call_count, 3)
        return got

    def test_an_old_probe_is_whole_not_an_alert(self):
        issues, reason = self.read(([item(1, "2026-10-01T00:00:00Z")], ""))
        self.assertEqual((len(issues), reason), (200, ""))

    def test_an_in_window_or_undated_probe_still_exceeds(self):
        # Guard: anything that does not prove the probe older keeps the conservative answer.
        for probe in ([item(1)], [{"number": 1}], [{"number": 1, "created_at": "garbage"}]):
            with self.subTest(probe=probe):
                issues, reason = self.read((probe, ""))
                self.assertEqual(len(issues), 200)
                self.assertEqual(reason, "open issue listing exceeds 2 full pages")


class BudgetStopsPaging(unittest.TestCase):
    """#1870: a keep-partial listing whose budget cannot fit another page stops with the pages
    read and a reason, instead of raising out of the sweep mid-listing."""

    def test_a_short_budget_stops_before_the_next_page(self):
        with gh.gate_scope(time.monotonic() + 5.0, per_call=20.0), \
                mock.patch.object(gh, "fetch", return_value=(full(0), "")) as fetch:
            issues, reason = gh.open_issues_read(LOOP, SINCE)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(issues), 100)
        self.assertRegex(reason, r"^open issue page 2 not read: \ds of the budget left$")
        self.assertFalse(gh.persistent_failure(reason))       # transient: the next sweep retries

    def test_an_ample_budget_or_none_reads_on(self):
        # Guard: only a budget under one call's cap stops it.
        for scope in (gh.gate_scope(time.monotonic() + 600.0, per_call=20.0), None):
            with self.subTest(budget=scope is not None):
                pages = iter([(full(0), ""), ([item(1)], "")])
                with (scope or mock.MagicMock()), \
                        mock.patch.object(gh, "fetch", side_effect=lambda *a, **k: next(pages)):
                    issues, reason = gh.open_issues_read(LOOP, SINCE)
                self.assertEqual((len(issues), reason), (101, ""))

    def test_a_listing_that_must_be_whole_is_unchanged(self):
        # Guard: without keep_partial a cut listing would read as unknown; the budget still raises.
        with gh.gate_scope(time.monotonic() + 5.0, per_call=20.0), \
                mock.patch.object(gh, "fetch", side_effect=[(full(0), ""), ([], "")]) as fetch:
            got = gh._read_pages(LOOP, "/x?per_page=100", "label", 5)
        self.assertEqual((len(got[0]), got[1], fetch.call_count), (100, "", 2))


if __name__ == "__main__":
    unittest.main()
