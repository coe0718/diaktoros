"""The gate package (#1082): ``gate.py`` is ``gate/__init__.py``, and ``gate_decisions``,
``gate_failures`` and ``gate_shims`` are ``gate/decisions.py``, ``failures.py`` and ``shims.py``.

The gate starts repository scripts by path, found from its own file, and so do the shims the
host installs (``shims.plugin_script``) and the failure re-drives (``failures.SCRIPTS_DIR``). Each start is best effort
(a failure is only logged), so a path left pointing at the old flat layout would fail quietly.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import conflict_scan, gate, route_intent  # noqa: E402
from diaktoros.gate import failures as gate_failures  # noqa: E402
from diaktoros.gate import shims as gate_shims  # noqa: E402


class Package(unittest.TestCase):
    def test_the_gate_is_the_package_not_a_flat_module(self):
        # What the script starts below are checked from (#2492 review 1, F1).
        self.assertEqual(pathlib.Path(gate.__file__), ROOT / "diaktoros" / "gate" / "__init__.py")
        self.assertEqual(list(gate.__path__), [str(ROOT / "diaktoros" / "gate")])
        self.assertFalse((ROOT / "diaktoros" / "gate.py").exists())

    def test_the_gates_modules_are_the_packages_own(self):
        # #1082 step 2: each lives in the package, and no flat copy is left to shadow it.
        from diaktoros.gate import decisions
        for name, module in (("decisions", decisions), ("failures", gate_failures),
                             ("shims", gate_shims)):
            with self.subTest(name):
                self.assertEqual(pathlib.Path(module.__file__), ROOT / "diaktoros" / "gate" / f"{name}.py")
                self.assertIs(getattr(gate, name), module)
                self.assertFalse((ROOT / "diaktoros" / f"gate_{name}.py").exists())

    def test_the_shadow_rules_live_only_in_their_module(self):
        # #1144: the shadow review's rules left gate/__init__.py for gate/shadows.py. One home
        # each, so a caller or a patch on the old name fails loudly instead of missing the code.
        from diaktoros.gate import shadows
        self.assertEqual(pathlib.Path(shadows.__file__), ROOT / "diaktoros" / "gate" / "shadows.py")
        self.assertIs(gate.shadows, shadows)
        own = [name for name, value in vars(shadows).items()
               if getattr(value, "__module__", None) == shadows.__name__]
        self.assertIn("shadow_state", own)
        self.assertIn("enqueue_shadow", own)
        self.assertEqual([name for name in own if hasattr(gate, name)], [])

    def test_the_shadow_rules_read_the_gate_core_through_gate(self):
        # So a test that patches a core rule on ``gate`` (``breach``, ``enqueue_isolated``…)
        # patches it for the shadow rules too: no core function is copied into their module.
        from diaktoros.gate import shadows
        copied = [name for name, value in vars(shadows).items()
                  if getattr(value, "__module__", None) == gate.__name__]
        self.assertEqual(copied, [])
        with mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as enqueue, \
                mock.patch.object(shadows, "upheld_shadow_ruling", return_value=1.0), \
                mock.patch.object(shadows.config, "unattended_fixer_push_enabled", return_value=True), \
                mock.patch.object(shadows, "_review_only_pr", return_value=False):
            outcome = shadows.queue_shadow_fix({"repo": "acme/widgets", "id": "w"}, 7, "a" * 40)
        self.assertEqual((outcome, enqueue.call_args.args[1:3]), ("enqueued", ("fixer", 7)))


class ScriptsTheGateStarts(unittest.TestCase):
    def started(self, call, how: str) -> pathlib.Path:
        with mock.patch.object(gate.subprocess, how) as start:
            start.return_value = mock.MagicMock(stdout="", stderr="", returncode=0)
            call()
        return pathlib.Path(start.call_args.args[0][1])

    def test_each_script_the_gate_starts_is_the_repositorys_own(self):
        loop = {"id": "widgets"}
        with mock.patch.object(gate.gh, "remaining", return_value=None):
            drain = self.started(lambda: gate.drain_seat(loop, "reviewer"), "run")
        with mock.patch.object(conflict_scan, "request"):
            scan = self.started(lambda: gate.schedule_conflict_scan(loop, None), "Popen")
        with mock.patch.object(gate, "log"):
            cleanup = self.started(lambda: gate.reclaim(loop, 7, "merged"), "run")
        self.assertEqual([drain, scan, cleanup], [ROOT / "scripts" / "watchdog.py",
                                                  ROOT / "scripts" / "watchdog.py",
                                                  ROOT / "scripts" / "cleanup.py"])
        for path in (drain, scan, cleanup):
            self.assertTrue(path.is_file(), path)

    def test_the_shims_and_redrives_run_the_repositorys_scripts(self):
        # An installed shim runs plugin_script(<gate script>); a failure re-drive looks there too.
        self.assertEqual(gate_failures.SCRIPTS_DIR, ROOT / "scripts")
        for script in sorted(set(route_intent.GATE_SCRIPT.values())):
            with self.subTest(script):
                self.assertEqual(gate_shims.plugin_script(script), ROOT / "scripts" / script)
                self.assertTrue(gate_shims.plugin_script(script).is_file())


if __name__ == "__main__":
    unittest.main()
