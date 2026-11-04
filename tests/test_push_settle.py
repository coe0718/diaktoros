"""#1763 part 2: the watchdog settles a fixer push quarantine that provably wrote nothing.

Driven through the sweep's own step (``watchdog.retire_stale_runs``, which runs with a complete
open-PR listing) on a real ledger. GitHub's branch read is mocked; the broker audit is a real
file. A quarantine is settled only when every proof holds; each case below takes one away.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import config, gh, ledger, push_settle, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor, workers  # noqa: E402
from scripts import watchdog  # noqa: E402

REPO, HEAD, MOVED = "acme/widgets", "a" * 40, "b" * 40
# A matching attempt whose time is written as AT, raw: not every value json reads is JSON's.
AT = f'{{"repo": "{REPO}", "pr": 7, "old_head": "{HEAD}", "phase": "attempt", "at": AT}}\n'


class Settle(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = config.host_path("ledger")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (self.db, self.db.with_name(self.db.name + "-wal")):
            leftover.unlink(missing_ok=True)
        self.addCleanup(self.db.unlink, missing_ok=True)
        self.sup = Supervisor(self.db)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "state_dir": str(self.root),
                     "read_token": "read", "unattended_fixer_push": True, "fixers": ["fix"],
                     "reviewers": ["review"], "seats": {"fixer": {"login": "fix"}}}
        self.ref = HEAD
        for patch in (mock.patch.object(gh, "api", side_effect=self.api),
                      mock.patch.object(config, "by_repo", return_value=self.loop),
                      mock.patch.object(workers, "process_state", return_value="gone")):
            patch.start()
            self.addCleanup(patch.stop)

    def api(self, loop, path, method="GET", body=None, login=None):
        if path == f"/repos/{REPO}/git/ref/heads/fix-7":
            return None if self.ref is None else {"ref": "refs/heads/fix-7",
                                                  "object": {"sha": self.ref}}
        raise AssertionError(f"unexpected read {path}")

    def held(self, error="post-write push quarantine: unresolved push intent", pid=4242):
        self.sup.enqueue("fix", REPO, 7, HEAD, "fixer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='uncertain', error=?, launch_intent=1, pid=?, "
                        "push_admitted=1, push_intent=? WHERE delivery='fix'",
                        (error, pid, time.time()))
        return self.sup.get("fix")["id"]

    def sweep(self, head=HEAD):
        lines = []
        listing = [{"number": 7, "head": {"sha": head, "ref": "fix-7"}}]
        watchdog.retire_stale_runs(self.loop, listing, lines)
        return lines

    def audit(self, **record):
        with open(self.root / "broker-audit.jsonl", "a") as audit:
            audit.write(json.dumps({"repo": REPO, "pr": 7, "old_head": HEAD, "phase": "attempt",
                                    "at": time.time(), **record}) + "\n")

    def test_a_held_push_whose_branch_never_moved_is_settled_and_can_be_retried(self):
        run_id = self.held()
        lines = self.sweep()
        row = self.sup.get("fix")
        self.assertEqual(row["state"], "failed")
        self.assertTrue(row["error"].startswith(run_supervisor.PUSH_SETTLED), row["error"])
        self.assertIsNone(row["push_intent"])
        self.assertFalse(self.sup.post_write_hold(REPO, 7))
        self.assertEqual(len(lines), 1)
        self.assertIn("is settled", lines[0])
        self.assertEqual(self.sup.retry(run_id), "pending")       # unlike a reconciliation
        self.assertEqual(self.sweep(), [])                         # nothing left to settle

    def test_an_audit_of_other_pushes_does_not_hold_it(self):
        self.held()
        self.audit(pr=8)                                           # another PR
        self.audit(old_head=MOVED)                                 # another head
        self.audit(phase="reconciled")                             # not an attempt
        self.audit(at=time.time() - 86400)                         # before this run began
        self.sweep()
        self.assertEqual(self.sup.get("fix")["state"], "failed")

    def test_anything_unproven_stays_held(self):
        cases = {
            "the branch moved": dict(ref=MOVED),
            "the branch is unreadable": dict(ref=None),
            "the PR head moved": dict(head=MOVED),
            "the PR is not in the listing": dict(listing=False),
            "an attempt is in the audit": dict(audit={}),
            "an audit line is unreadable": dict(audit_raw="{not json\n"),
            "a worker may be alive": dict(process="alive"),
            "the worker cannot be checked": dict(process="unknown"),
            "the host saw it publish": dict(error="post-write push quarantine: published"),
            "it is not a push quarantine": dict(error="lease expired"),
            # Review of #1802, F2: the launch is recorded before its PID; none is not gone.
            "no worker PID was recorded": dict(pid=None),
            # F3: an attempt it cannot date, or a line it cannot read as a record.
            "an attempt has no time": dict(audit={"at": None}),
            "an audit line is not a record": dict(audit_raw="[1]\n"),
            # F4: json reads NaN and an overflow (-1e999 is -inf) as floats no time compares to.
            "an attempt at NaN": dict(audit_raw=AT.replace("AT", "NaN")),
            "an attempt at -1e999": dict(audit_raw=AT.replace("AT", "-1e999")),
            "an attempt at -Infinity": dict(audit_raw=AT.replace("AT", "-Infinity")),
            # #1828: a JSON integer stays an int, and float(10**400) raises OverflowError.
            "an attempt at 10**400": dict(audit_raw=AT.replace("AT", "1" + "0" * 400)),
            "an attempt at -10**400": dict(audit_raw=AT.replace("AT", "-1" + "0" * 400)),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.setUp()
                self.held(error=case.get("error", "post-write push quarantine: unknown"),
                          pid=case.get("pid", 4242))
                self.ref = case.get("ref", HEAD)
                if "audit" in case:
                    self.audit(**case["audit"])
                if "audit_raw" in case:
                    (self.root / "broker-audit.jsonl").write_text(case["audit_raw"])
                with mock.patch.object(workers, "process_state",
                                       return_value=case.get("process", "gone")):
                    if case.get("listing") is False:
                        watchdog.retire_stale_runs(self.loop, [], [])
                    else:
                        lines = self.sweep(head=case.get("head", HEAD))
                        # Held quietly: the watchdog catches an exception from the sweep and
                        # reports it, which is not the same answer (#1828).
                        self.assertEqual(lines, [])
                row = self.sup.get("fix")
                self.assertEqual(row["state"], "uncertain", row["error"])
                self.assertTrue(self.sup.post_write_hold(REPO, 7))

    def test_a_worker_is_gone_before_any_proof_is_read(self):
        # Review of #1802, F1: the audit and the ref were read first, and the worker checked
        # after. A late worker writes its attempt while the host checks it, and dies: read
        # first, that attempt was missed and the push settled as nothing written.
        self.held()

        def late_worker(pid, launched):
            if not (self.root / "broker-audit.jsonl").exists():
                self.audit()                          # its attempt, then it is gone
            return "gone"
        with mock.patch.object(workers, "process_state", side_effect=late_worker):
            self.sweep()
        self.assertEqual(self.sup.get("fix")["state"], "uncertain")
        self.assertTrue(self.sup.post_write_hold(REPO, 7))

    def test_a_ledger_with_no_held_push_reads_nothing_from_github(self):
        self.sup.enqueue("fix", REPO, 7, HEAD, "fixer")
        with mock.patch.object(push_settle.gh, "api", side_effect=AssertionError("no read")):
            self.assertEqual(self.sweep(), [])


if __name__ == "__main__":
    unittest.main()
