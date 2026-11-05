"""De-facto public helpers have public names (#1088).

State's key-value read/write, its breach lock and minutes knob, config's path and number
validators, and selftest's work root were reached past the underscore by other modules (gate,
install, merge_check, ready, migrate, ...). They are now public, and no module outside the one
that defines them may reach the private spelling again.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, selftest, state  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

# private attribute -> the files allowed to use it (its own module)
PRIVATE = {
    "_load": {"diaktoros/state.py"},
    "_save": {"diaktoros/state.py"},
    "_save_strict": {"diaktoros/state.py"},
    "_breach_lock": set(),
    "_minutes": set(),
    "_form_int": set(),
    "_check_budget": set(),
    "_check_max_steps": set(),
    "_check_daily_turns": set(),
    "_check_daily_tokens": set(),
    "_work_root": set(),
}
# receivers whose ``_load``/``_save`` are their own module's, not LoopState's
OTHER_OWNERS = {"onboarding", "observer", "pacing", "Ledger", "self"}


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


class PublicNames(unittest.TestCase):
    def test_public_names_exist(self):
        for name in ("form_int", "validate_budget", "validate_max_steps",
                     "validate_daily_turns", "validate_daily_tokens", "expand_path"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(config, name, None)))
        self.assertTrue(callable(getattr(selftest, "work_root", None)))
        for name in ("kv_get", "kv_set", "kv_set_strict", "breach_lock", "minutes"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(state.LoopState, name, None)))

    def test_validators_keep_their_rules(self):
        self.assertEqual(config.form_int(" 12 "), 12)
        self.assertEqual(config.form_int("x"), "x")
        self.assertEqual(config.validate_max_steps(40, "seats.fixer.max_steps", "t"), 40)
        for check in (config.validate_max_steps, config.validate_daily_turns,
                      config.validate_daily_tokens, config.validate_budget):
            with self.subTest(check=check.__name__), self.assertRaises(config.ConfigError):
                check(True, "what", "t")

    def test_kv_round_trip_and_breach_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            loop = {"id": "w", "repo": "acme/widgets", "state_dir": temp,
                    "fixers": ["f"], "reviewers": ["r"], "seats": {"reviewer": {}, "fixer": {}}}
            st = state.LoopState(loop)
            path = st.dir / "kv.json"
            self.assertEqual(st.kv_get(path, {"none": 1}), {"none": 1})
            with st.breach_lock():
                st.kv_set(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            st.kv_set_strict(path, {"b": 2})
            self.assertEqual(st.kv_get(path, {}), {"b": 2})

    def test_no_module_reaches_the_private_spelling(self):
        files = [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
        found = []
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Attribute) or node.attr not in PRIVATE:
                    continue
                if rel in PRIVATE[node.attr] or _receiver(node.value) in OTHER_OWNERS:
                    continue
                found.append(f"{rel}:{node.lineno} .{node.attr}")
        self.assertEqual(found, [], "use the public name (#1088)")

    def test_config_path_is_not_reached_privately(self):
        files = [*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
        found = [f"{p.relative_to(ROOT)}:{n.lineno}" for p in files
                 for n in ast.walk(ast.parse(p.read_text()))
                 if isinstance(n, ast.Attribute) and n.attr == "_path"
                 and _receiver(n.value) == "config"]
        self.assertEqual(found, [], "use config.expand_path (#1088)")


if __name__ == "__main__":
    unittest.main()
