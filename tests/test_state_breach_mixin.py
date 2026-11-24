"""The breach markers live in ``state.breach`` as a ``LoopState`` mixin (split from
``state/__init__.py`` at its size ceiling), and reach the state module's helpers through the
package at call time, so a patch on ``diaktoros.state`` still reaches a breach write.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import state  # noqa: E402
from diaktoros.state import breach  # noqa: E402

BREACH = ("breach_lock", "_breach_save", "breach_get", "breach_set", "breach_deliver",
          "breach_clear_pending", "breach_claim", "breach_start", "breach_resume",
          "breach_reopen", "breach_all")


class BreachMixin(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "inflight_ttl_min": 10, "ttl_min": 45, "turn_budget_s": 900,
                     "state_dir": str(Path(temp.name) / "state"), "fixers": ["f"],
                     "reviewers": ["r"], "seats": {"reviewer": {}, "fixer": {}}}
        self.st = state.LoopState(self.loop)
        self.st.dir.mkdir(parents=True, exist_ok=True)

    def test_every_breach_method_comes_from_the_mixin(self):
        self.assertTrue(issubclass(state.LoopState, breach.Breaches))
        for name in BREACH:
            self.assertIn(name, vars(breach.Breaches), name)
            self.assertNotIn(name, vars(state.LoopState), f"{name}: a second copy in LoopState")

    def test_a_patch_on_the_state_module_reaches_a_breach_write(self):
        with mock.patch.object(state, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.st.breach_set(7, {"head": "a" * 40, "status": "pending"})
        self.assertEqual(self.st.breach_get(7), {})
        self.st.breach_set(7, {"head": "a" * 40, "status": "pending"})
        self.assertEqual(self.st.breach_get(7).get("status"), "pending")


if __name__ == "__main__":
    unittest.main()
