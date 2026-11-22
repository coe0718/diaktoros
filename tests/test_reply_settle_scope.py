"""#2569: a worker waits out settling reply rows of its own repo only.

``settling()`` (#2248) asked whether any row in the ledger waited on ``SETTLE_HOLD``, so every
worker of every loop lingered up to ``_linger_until_due``'s window after its turn whenever any
loop had a settling reply row. Now a submit that queues a settling row spawns its worker told
that row's repo (``SETTLE_REPO_ENV``), and a worker counts settling rows of that repo only, both
for whether to linger and for what to wait for while it does.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_issue_fix as base  # noqa: E402
import test_reply_settle as settle  # noqa: E402
BASE, REPO, row = base.BASE, base.REPO, settle.row

from diaktoros import envnames, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor, workers  # noqa: E402

OTHER = "other/repo"


def load_tests(loader, standard_tests, pattern):
    """This file's own tests only (``base.Base``'s setUp is borrowed, not its tests)."""
    suite = unittest.TestSuite()
    for case in (Scope, SpawnedFor, WorkerEntry):
        names = sorted(n for n in vars(case) if n.startswith("test_"))
        suite.addTests(case(name) for name in names)
    return suite


class Scoped(base.Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(Supervisor, "_spawn")
        self.spawn = patch.start()
        self.addCleanup(patch.stop)

    def worker(self, *repos) -> Supervisor:
        """A fresh worker on the ledger, spawned for ``repos``' settling rows (none: any other
        worker), with recover() recorded rather than run."""
        sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        sup.settle_repos = frozenset(repos)
        sup.recover = mock.Mock()
        return sup

    def settle_row(self, repo, delivery, wait=30):
        submitter = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        submitter.submit(delivery, repo, 99, BASE, "issue_fixer",
                         turn_key=f"issue-fix:reply-{delivery}", settle=wait)
        self.assertEqual(row(self.db, delivery)["error"], run_supervisor.SETTLE_HOLD)


class Scope(Scoped):
    def test_another_repos_settling_row_is_not_this_workers(self):
        self.settle_row(OTHER, "d2")
        self.assertFalse(self.worker().settling())          # the issue's probe: was True
        self.assertFalse(self.worker(REPO).settling())
        self.assertTrue(self.worker(OTHER).settling())

    def test_a_worker_with_no_settling_row_of_its_own_does_not_linger(self):
        self.settle_row(OTHER, "d2")
        sup = self.worker(REPO)
        sup.linger_for_ci(sleep=self.fail)
        sup.recover.assert_not_called()

    def test_a_worker_held_for_ci_does_not_wait_for_another_repos_settling_row(self):
        self.settle_row(OTHER, "d2")
        sup = self.worker()
        sup.held_for_ci = True                              # its own CI hold is already gone
        sup.linger_for_ci(sleep=self.fail)
        sup.recover.assert_not_called()

    def test_a_worker_waits_for_its_own_repos_settling_row(self):
        self.settle_row(OTHER, "d2")
        self.settle_row(REPO, "d1")
        sup, slept = self.worker(REPO), []

        def sleep(seconds):
            slept.append(seconds)
            settle.fall_due(self.db, "d1")
        sup.linger_for_ci(sleep=sleep)
        self.assertEqual(len(slept), 1)
        sup.recover.assert_called_once()


class SpawnedFor(Scoped):
    def submit(self, wait, delivery="d", repo=REPO) -> str:
        sup = self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        return sup.submit(delivery, repo, 12, BASE, "issue_fixer",
                          turn_key="issue-fix:reply1", settle=wait)

    def test_a_settling_submit_spawns_its_worker_for_that_repo(self):
        self.assertEqual(self.submit(120), "enqueued")
        self.spawn.assert_called_once_with(settle_repo=REPO)
        self.assertEqual(self.sup.settle_repos, {REPO})

    def test_a_redelivery_of_a_settling_row_spawns_for_its_repo_too(self):
        self.submit(120)
        self.spawn.reset_mock()
        self.assertTrue(self.submit(120).startswith("duplicate waiting"))
        self.spawn.assert_called_once_with(settle_repo=REPO)

    def test_a_runnable_submit_spawns_an_unscoped_worker_as_before(self):
        self.assertEqual(self.submit(0), "enqueued")
        self.spawn.assert_called_once_with()
        self.assertEqual(self.sup.settle_repos, frozenset())

    def test_the_spawned_worker_is_handed_the_repo(self):
        sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        spawn = workers.WorkerLifecycle._spawn          # the real one, its process faked
        with mock.patch("diaktoros.run_supervisor.subprocess.Popen") as popen:
            spawn(sup, settle_repo=REPO)
            spawn(sup)
        scoped, plain = (call.kwargs["env"] for call in popen.call_args_list)
        self.assertEqual(scoped.get(workers.SETTLE_REPO_ENV), REPO)
        self.assertNotIn(workers.SETTLE_REPO_ENV, plain)



class WorkerEntry(base.Base):
    """The spawned worker process reads its repo before it claims and lingers."""

    def run_worker(self, env: dict) -> frozenset:
        seen = []
        Supervisor(self.db)                     # the host made the ledger; a worker never does
        args = argparse.Namespace(operation="_fixture-worker", db=str(self.db),
                                  command=json.dumps(["true"]), capacity=json.dumps({}),
                                  lease=30.0, timeout=30.0)
        with mock.patch.dict(os.environ, {envnames.name("TEST_FIXTURE"): "1", **env}), \
                mock.patch.object(run_supervisor.waits, "cap_stderr"), \
                mock.patch.object(Supervisor, "_run_one"), \
                mock.patch.object(Supervisor, "linger_for_ci",
                                  lambda sup: seen.append(sup.settle_repos)):
            run_supervisor._worker(args)
        return seen[0]

    def test_a_worker_spawned_for_a_settling_row_lingers_for_its_repo(self):
        self.assertEqual(self.run_worker({workers.SETTLE_REPO_ENV: REPO}), {REPO})

    def test_any_other_worker_has_no_settling_repo(self):
        self.assertEqual(self.run_worker({}), frozenset())


if __name__ == "__main__":
    unittest.main()
