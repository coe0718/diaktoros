"""#1349 split of ``gate.py``: ``explain`` and its helpers live in ``diaktoros/explain.py``.

A pure move: one home for each name, no re-export left in ``gate``, and nothing anywhere still
reaching the old home (``gate.explain``, ``diaktoros.gate._explain_state``, a patch on
``gate``). The gate predicates ``explain`` asks stay in ``gate``, so ``explain`` reads the rules
the gates run and keeps no copy of them.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOVED = ("LIVE_TURN", "EXPLAIN_KINDS", "_mark_time", "_minutes_since", "_adjudication_next", "_ruled",
         "_explain_state", "_explain_app_ingress", "_explain_hooks", "explain_facts", "_owner_note",
         "gate_failure_line", "explain", "authorship_lines")
SHARED = ("seat_capacity", "STANDING", "escalated", "breach_holds", "breach_delivery_status")


def top_level(path: pathlib.Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


class OneHome(unittest.TestCase):
    def test_each_moved_name_is_defined_in_explain_only(self):
        gate, explain = top_level(ROOT / "diaktoros/gate.py"), top_level(ROOT / "diaktoros/explain.py")
        for name in MOVED:
            with self.subTest(name):
                self.assertIn(name, explain)
                self.assertNotIn(name, gate)
        for name in SHARED:                   # the predicates the gates run stay with the gates
            with self.subTest(name):
                self.assertIn(name, gate)
                self.assertNotIn(name, explain)

    def test_nothing_reaches_a_moved_name_through_gate(self):
        moved = "|".join(MOVED)
        old = re.compile(rf"(?<![\w.])gate\.({moved})\b|\bdiaktoros\.gate\.({moved})\b"
                         rf"|\(gate, \"({moved})\"")
        for root in ("diaktoros", "scripts", "tests", "dashboard", "docs"):
            for path in sorted((ROOT / root).rglob("*")):
                if path.suffix not in (".py", ".md") or path == pathlib.Path(__file__):
                    continue
                with self.subTest(path.relative_to(ROOT).as_posix()):
                    self.assertEqual([m.group(0) for m in old.finditer(path.read_text())], [])


if __name__ == "__main__":
    unittest.main()
