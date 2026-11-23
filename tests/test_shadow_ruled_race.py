"""#2603 review F1: a shadow's request and its ruling's copy of the same finding, sent by two
sweeps that interleave, are never both filed.

Each sweep reads the other seat's copy before it claims (``ruled_copy``, ``_shadow_asked``), but
the other may claim or file in between. So the claim's own transaction (``record_shadow_filing``)
reads it again, by place as well as title: a copy filed (or maybe filed) since makes this one a
``duplicate``; one whose POST is under way sends it back to ``recorded`` to wait. Each test pauses
one sweep right after that first read and runs the other there, as the review's probe did.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shadow_ruled_fallback as fallback  # noqa: E402
from diaktoros import ruling_issues  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

RULING_BODY = "`diaktoros/history.py:44-50` promises exit 2"
ON = {"profile": "p", "file_issues": True}
OFF = {"profile": "p"}


class Interleaved(unittest.TestCase):
    setUp = fallback.RuledFallback.setUp
    recorded, state = fallback.RuledFallback.recorded, fallback.RuledFallback.state
    ruled_over_shadow = fallback.RuledFallback.ruled_over_shadow
    shadow_state = fallback.RuledFallback.shadow_state
    _shadow_sweep = fallback.RuledFallback.shadow_sweep
    _ruling_sweep = fallback.RuledFallback.file

    def shadow_sweep(self):
        return self._shadow_sweep()                    # with shadow.file_issues on

    def ruling_sweep(self, shadow):
        """The adjudicator's sweep, with ``shadow.file_issues`` as ``shadow``: what it POSTed."""
        self.loop = {**self.loop, "shadow": shadow}
        return [post[1]["title"] for post in self._ruling_sweep()[1]]

    def paused(self, name, during):
        """``ruling_issues.<name>`` whose first read, the sweep's own before its claim, runs
        ``during`` right after it is read: the other sweep, between that read and the claim."""
        real, calls = getattr(ruling_issues, name), []

        def pause(*args, **kwargs):
            found = real(*args, **kwargs)
            if kwargs.get("con") is None and not calls:
                calls.append(found)
                during()
            return found
        return mock.patch.object(ruling_issues, name, side_effect=pause), calls

    def mid_claim(self, run):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE shadow_issues SET state='posting', updated=? WHERE run_id=?",
                        (time.time(), run))

    def test_a_ruling_filed_between_the_shadows_read_and_its_claim(self):
        # The review's probe: the ruling sweep started while shadow filing was off files its
        # fallback after the shadow sweep read none; the shadow's claim must see it.
        run = self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        ruled = []
        patch, calls = self.paused("ruled_copy", lambda: ruled.extend(self.ruling_sweep(OFF)))
        with patch:
            self.assertEqual(self.shadow_sweep(), [])
        self.assertEqual((calls, ruled), ([None], [fallback.dedup.RULING_TITLE]))
        self.assertEqual((self.state(run), self.shadow_state()), ("posted", "duplicate"))

    def test_a_ruling_mid_claim_between_the_shadows_read_and_its_claim(self):
        run = self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        patch, calls = self.paused("ruled_copy", lambda: self.mid_claim(run))
        with patch:
            self.assertEqual(self.shadow_sweep(), [])
        self.assertEqual(calls, [None])
        self.assertEqual(self.shadow_state(), "recorded")             # waits for its outcome

    def test_a_shadow_filed_between_the_rulings_read_and_its_claim(self):
        # The other order: the ruling sweep (filing off) read the shadow's request as never to
        # be filed, then a shadow sweep (filing on) filed it before the ruling's claim.
        run = self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        shadowed = []
        patch, calls = self.paused("_shadow_asked", lambda: shadowed.extend(self.shadow_sweep()))
        with patch:
            self.assertEqual(self.ruling_sweep(OFF), [])
        self.assertEqual((calls, shadowed), ([None], [fallback.dedup.SHADOW_TITLE]))
        self.assertEqual((self.shadow_state(), self.state(run)), ("posted", "duplicate"))

    def test_a_shadow_mid_claim_between_the_rulings_read_and_its_claim(self):
        run = self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        patch, calls = self.paused("_shadow_asked", lambda: self.mid_claim("s1"))
        with patch:
            self.assertEqual(self.ruling_sweep(OFF), [])
        self.assertEqual(calls, [None])
        self.assertEqual(self.state(run), "recorded")                  # waits for its outcome

    def test_with_no_interleaving_each_side_still_files_alone(self):
        # Guard: the recheck holds nothing when the other seat's copy is not taken.
        run = self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        self.assertEqual(self.ruling_sweep(OFF), [fallback.dedup.RULING_TITLE])
        self.assertEqual(self.state(run), "posted")
        self.setUp()
        self.ruled_over_shadow(RULING_BODY, fallback.CITED)
        self.assertEqual(self.shadow_sweep(), [fallback.dedup.SHADOW_TITLE])


if __name__ == "__main__":
    unittest.main()
