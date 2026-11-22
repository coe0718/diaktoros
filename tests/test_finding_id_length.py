"""Review on #2589, F6: a finding id the review parser accepts is one the safety checks see.

The parser takes ``F`` and any run of digits, so an open ``F`` + 19 digits (past the 18 that
``int`` was bounded to) or + 4,301 digits (past ``int``'s conversion limit) is recorded open. The
APPROVE guard and the later-round omission guard read the same ids through ``open_ids``: an
approval over it is refused, at ``findings.check`` and through the broker, and every view that
lists findings keeps it in number order without raising.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from unittest import mock

import test_broker_ipc as tbi
from diaktoros import ci, finding_answers, findings, history, state

LONG = ("F" + "9" * 19, "F" + "9" * 4301)
LOOP = {"repo": "o/r"}


def raised(fid: str) -> dict:
    return findings.apply({}, "h1", f"{fid} [correctness]: src/a.py:1: a real bug\n")


class Checks(unittest.TestCase):
    def test_a_long_id_is_recorded_open_and_listed_open(self):
        for fid in LONG:
            with self.subTest(len(fid)):
                entry = raised(fid)
                self.assertEqual(entry["findings"][fid]["state"], "open")
                self.assertEqual(findings.open_ids(entry), [fid])

    def test_an_approval_over_a_long_open_id_is_refused(self):
        for fid in LONG:
            with self.subTest(len(fid)):
                reason = findings.check(LOOP, raised(fid), 7, "h2", "looks fine", "APPROVE")
                self.assertIn(f"cannot APPROVE with open finding(s) {fid}", reason)
                self.assertEqual(findings.check(LOOP, raised(fid), 7, "h2", f"{fid}: fixed\n",
                                                "APPROVE"), "")

    def test_a_later_round_that_leaves_a_long_id_out_is_refused(self):
        for fid in LONG:
            with self.subTest(len(fid)):
                reason = findings.check(LOOP, raised(fid), 7, "h2",
                                        "F1 [correctness]: src/b.py:2: other\n", "REQUEST_CHANGES")
                self.assertIn(f"open finding(s) {fid} left out", reason)

    def test_long_ids_sort_after_short_ones_everywhere(self):
        entry = findings.apply({}, "h1", "".join(f"{fid} [correctness]: src/a.py:1: x\n"
                                                 for fid in (*reversed(LONG), "F10", "F2")))
        want = ["F2", "F10", *LONG]
        self.assertEqual(findings.open_ids(entry), want)
        self.assertEqual([line.split()[0] for line in findings.lines(entry)], want)
        self.assertEqual(sorted(entry["findings"], key=findings.order), want)
        self.assertEqual(sorted(want, key=history.findings_mod.order), want)

    def test_answers_to_a_long_open_id_are_listed_and_checked(self):
        # ``open_ids`` now hands these ids on; the answers section and refusal sort them too.
        entry = findings.apply({}, "h1", f"F2 [correctness]: src/a.py:1: x\n{LONG[1]} [x]: y\n")
        said = {"stamp": "2026-10-14T10:00:00Z", "who": "boss", "where": "issue #5", "body": "ok"}
        got = {LONG[1]: [said], "F2": [said]}
        with mock.patch.object(finding_answers, "answered", return_value=got):
            text = finding_answers.section(LOOP, 7, entry)
            self.assertLess(text.index("### F2"), text.index(f"### {LONG[1]}"))
            reason = finding_answers.refusal(LOOP, 7, entry, {LONG[1]: "", "F2": ""})
        self.assertIn("finding F2 was answered", reason)


class ThroughTheBroker(tbi.BrokerIPCBase):
    def test_the_broker_refuses_an_approval_over_a_long_open_id(self):
        for fid in LONG:
            with self.subTest(len(fid)):
                self.calls.clear()
                findings.record(self.loop, 7, "c" * 40, f"{fid} [correctness]: src/a.py: leak\n")
                server = self.start()
                with mock.patch.object(ci, "read", return_value=ci.CIState()):
                    refused = self.send(server, {"operation": "review", "verdict": "APPROVE",
                                                 "body": "looks fine\nNot verified: nothing\n"})
                self.assertFalse(refused["ok"])
                self.assertIn("cannot APPROVE", refused["error"])
                self.assertNotIn("POST", [c[1] for c in self.calls])   # nothing written
                st = state.state_for(self.loop)
                self.assertEqual(findings.open_ids(st.findings_get(7)), [fid])
                st.findings_put(7, {})


if __name__ == "__main__":
    unittest.main()
