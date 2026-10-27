#!/usr/bin/env python3
"""#1016: reviewer/fixer concurrency above 1 needs no ``clone`` once every run is isolated.

Every production turn is launched by the isolated worker, which needs the runtime file; with it
present nothing shares a checkout. Without it (and without a clone) the loader still refuses.
The legacy shared-checkout path, ``gate.take_seat``, still queues a run it cannot isolate.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import config, gate, state  # noqa: E402

LOOP = {"id": "one", "repo": "owner/one", "base": "main", "cap": 3, "fixers": ["fixer"],
        "reviewers": ["reviewer"], "reviewer_seat": "reviewer", "read_token": "reader",
        "seats": {"reviewer": {"route": "one-review", "profile": "reviewer"},
                  "fixer": {"route": "one-fix", "profile": "fixer"}}}


def loop_with(concurrency: int, clone: str = "") -> dict:
    raw = {**LOOP, "seats": {s: {**v} for s, v in LOOP["seats"].items()}}
    raw["seats"]["reviewer"]["concurrency"] = concurrency
    if clone:
        raw["clone"] = clone
    return raw


class IsolatedConcurrency(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DIAKTOROS_CONFIG_DIR", None)

    def write_runtime(self):
        path = config.host_path("runtime")
        path.write_text("{}")
        path.chmod(0o600)

    def test_accepted_without_clone_when_the_isolated_worker_is_configured(self):
        self.write_runtime()
        loop = config.normalize(loop_with(2))
        self.assertEqual(config.seat_concurrency(loop, "reviewer"), 2)

    def test_loop_level_concurrency_is_accepted_too(self):
        self.write_runtime()
        loop = config.normalize({**LOOP, "concurrency": 3})
        self.assertEqual(config.seat_concurrency(loop, "fixer"), 3)

    def test_refused_without_clone_and_without_the_runtime_file(self):
        with self.assertRaisesRegex(config.ConfigError, "seats.reviewer.concurrency > 1 requires 'clone'"):
            config.normalize(loop_with(2))
        with self.assertRaisesRegex(config.ConfigError, "runtime file"):
            config.normalize({**LOOP, "concurrency": 2})

    def test_clone_alone_still_suffices(self):
        loop = config.normalize(loop_with(2, clone=str(self.home / "clone")))
        self.assertEqual(config.seat_concurrency(loop, "reviewer"), 2)

    def test_shared_checkout_path_still_queues_an_unisolatable_parallel_run(self):
        """take_seat (the legacy shared-clone path) never starts an unisolated run above 1."""
        self.write_runtime()
        loop = config.normalize({**loop_with(2), "state_dir": str(self.home / "state")})
        st = state.LoopState(loop)
        st.dir.mkdir(parents=True, exist_ok=True)
        before = list(state.CLAIMS)
        self.addCleanup(lambda: state.CLAIMS.__setitem__(slice(None), before))
        with mock.patch.object(gate.isolation, "ensure", return_value=None):
            with self.assertRaises(SystemExit):
                gate.take_seat(loop, st, "reviewer", 7, "a" * 40, "why")
        self.assertIn(gate.seat_key(loop, 7), st.queue_items("reviewer"))
        self.assertNotIn(gate.seat_key(loop, 7), st.active("reviewer"))


if __name__ == "__main__":
    unittest.main()
