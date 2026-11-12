"""Review enumeration cannot silently turn a partial history into a verdict."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import gate, gh

HEAD = "a" * 40
LOOP = {"repo": "acme/widgets", "reviewers": ["reviewer"]}
PATH = "/repos/acme/widgets/pulls/7/reviews?per_page=100"


def verdict(rid: int, state: str, submitted: str = "2026-01-01T00:00:00Z") -> dict:
    return {"id": rid, "user": {"login": "reviewer"}, "state": state,
            "commit_id": HEAD, "submitted_at": submitted}


class ReviewPaginationTest(unittest.TestCase):
    def test_later_page_overturns_first_page_approval(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 100)] + [
            verdict(100, "APPROVED")]
        later = [verdict(101, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")]
        with mock.patch.object(gh, "fetch", side_effect=[(first, ""), (later, "")]) as fetch:
            reviews = gh.reviews(LOOP, 7)
        self.assertEqual(fetch.call_args_list, [mock.call(LOOP, PATH),
                                                mock.call(LOOP, PATH + "&page=2")])
        self.assertIsInstance(reviews, list)
        assert reviews is not None
        self.assertEqual(len(reviews), 101)
        latest = gate.latest_effective_review_at_head(reviews, LOOP, HEAD)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["state"], "CHANGES_REQUESTED")

    def test_exactly_full_page_probes_next_page_even_when_empty(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 101)]
        with mock.patch.object(gh, "fetch", side_effect=[(first, ""), ([], "")]) as fetch:
            self.assertEqual(gh.reviews_read(LOOP, 7), (first, ""))
        self.assertEqual(fetch.call_count, 2)

    def test_second_page_failure_discards_earlier_approval(self):
        first = [verdict(i, "COMMENTED") for i in range(1, 100)] + [
            verdict(100, "APPROVED")]
        for payload, error in ((None, "HTTP 500"), (None, ""),
                               ({"message": "rate limited"}, ""), ([None], "")):
            with self.subTest(payload=payload, error=error):
                with mock.patch.object(gh, "fetch", side_effect=[(first, ""),
                                                                 (payload, error)]):
                    result, reason = gh.reviews_read(LOOP, 7)
                self.assertIsNone(result)
                self.assertIn("page 2", reason)

    def test_oversized_page_and_unbounded_full_pages_fail_closed(self):
        first = [verdict(i, "COMMENTED") for i in range(101)]
        with mock.patch.object(gh, "fetch", return_value=(first, "")):
            self.assertIsNone(gh.reviews(LOOP, 7))
        with (mock.patch.object(gh, "MAX_REVIEW_PAGES", 2),
              mock.patch.object(gh, "fetch", return_value=(first[:100], "")) as fetch):
            result, reason = gh.reviews_read(LOOP, 7)
        self.assertIsNone(result)
        self.assertIn("exceeds", reason)
        self.assertEqual(fetch.call_count, 3)       # two pages and the probe past the bound

    def test_exactly_max_full_pages_with_an_empty_next_page_is_whole(self):
        page = [verdict(i, "COMMENTED") for i in range(100)]
        with (mock.patch.object(gh, "MAX_REVIEW_PAGES", 2),
              mock.patch.object(gh, "fetch",
                                side_effect=[(page, ""), (page, ""), ([], "")]) as fetch):
            result, reason = gh.reviews_read(LOOP, 7)
        self.assertEqual((result, reason), (page + page, ""))
        self.assertEqual(fetch.call_count, 3)

    def test_unreadable_probe_says_the_page_past_the_bound_could_not_be_read(self):
        # #1727: the probe past the bound errors or is malformed: the listing is not proven
        # whole (pages kept with keep_partial, None without), but it is not proven longer either,
        # so the reason names the unread page and the probe's error, never "exceeds".
        page = [verdict(i, "COMMENTED") for i in range(100)]
        unread = "review page 2, past the 1-page bound, could not be read"
        for payload, error, reason in (
                ([], "HTTP 500", f"{unread}: HTTP 500"),
                (None, "HTTP 500", f"{unread}: HTTP 500"),
                (None, "", f"{unread}: invalid review list"),
                ({"message": "rate limited"}, "", f"{unread}: invalid review list"),
                ([None], "", f"{unread}: invalid review list"),
                (page, "", "review listing exceeds 1 full pages")):
            for keep, expected in ((True, page), (False, None)):
                with self.subTest(payload=payload, error=error, keep_partial=keep):
                    with mock.patch.object(gh, "fetch",
                                           side_effect=[(page, ""), (payload, error)]) as fetch:
                        got = gh._read_pages(LOOP, PATH, "review", 1, keep_partial=keep)
                    self.assertEqual(got, (expected, reason))
                    self.assertEqual(fetch.call_count, 2)

    def test_open_issues_read_keeps_the_pages_when_the_probe_fails(self):
        page = [verdict(i, "COMMENTED") for i in range(100)]
        with (mock.patch.object(gh, "MAX_ISSUE_PAGES", 1),
              mock.patch.object(gh, "fetch", side_effect=[(page, ""), (None, "HTTP 500")])):
            got = gh.open_issues_read(LOOP, "2026-01-01T00:00:00Z")
        self.assertEqual(got, (page, "open issue page 2, past the 1-page bound, could not be "
                                     "read: HTTP 500"))
        # A transient probe failure reads as transient, like any other failed page (#1727).
        self.assertFalse(gh.persistent_failure(got[1]))

    def test_a_probe_page_with_items_still_exceeds_and_is_persistent(self):
        page = [verdict(i, "COMMENTED") for i in range(100)]
        with (mock.patch.object(gh, "MAX_ISSUE_PAGES", 1),
              mock.patch.object(gh, "fetch", side_effect=[(page, ""), (page[:1], "")])):
            got = gh.open_issues_read(LOOP, "2026-01-01T00:00:00Z")
        self.assertEqual(got, (page, "open issue listing exceeds 1 full pages"))
        self.assertTrue(gh.persistent_failure(got[1]))


class NoLooseApprovalPredicateTest(unittest.TestCase):
    def test_gate_has_no_unordered_approval_predicate(self):
        # #1061: ``approved_at_head`` was any(APPROVED at head), with no chronology: a later
        # CHANGES_REQUESTED did not override it. Callers use latest_effective_review_at_head.
        self.assertFalse(hasattr(gate, "approved_at_head"))
        reviews = [verdict(1, "APPROVED"), verdict(2, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z")]
        latest = gate.latest_effective_review_at_head(reviews, LOOP, HEAD)
        assert latest is not None
        self.assertEqual(latest["state"], "CHANGES_REQUESTED")


if __name__ == "__main__":
    unittest.main()
