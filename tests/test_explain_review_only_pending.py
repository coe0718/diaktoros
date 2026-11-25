"""#1889: a review-only head whose escalation's delivery is pending waits on that delivery.

``hermes dk escalate`` on a review-only PR whose adjudicator wake fails leaves the breach marker
at ``delivery-pending``. ``explain``'s next step is then the watchdog's delivery retry, as its
blockers say, and not the author's push: ``_next_ruling`` is read before ``_next_review_only``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_escalate as te  # noqa: E402
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, state as state_mod  # noqa: E402


class PendingReviewOnly(te.Base):
    explain = te.ParkedReviewOnly.explain

    def setUp(self):
        super().setUp()
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        data["review_only"] = ["owner-human"]
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.live = {**fg.LIVE, "user": {"login": "Owner-Human"}}
        self.assertEqual(self.escalate("person asks for ruling")[0], 0)
        marker = self.st.breach_get(7)
        self.assertTrue(marker.get("escalated"), marker)        # what holds it below the cap
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": {**marker,
                                                                   "status": "delivery-pending"}})

    def test_the_next_step_retries_the_delivery_not_the_authors_push(self):
        report = self.explain()
        self.assertLess(report["spent"], report["cap"])                # below the verdict cap
        self.assertTrue(any(b.startswith(f"adjudicator delivery pending at head {te.HEAD[:7]}")
                            for b in report["blockers"]), report["blockers"])
        self.assertEqual(report["next"]["kind"], "retry", report["next"])
        self.assertIn(f"retry adjudicator delivery for head {te.HEAD[:7]}", report["next"]["action"])
        self.assertNotIn("pushes a fix", report["next"]["action"])


if __name__ == "__main__":
    unittest.main()
