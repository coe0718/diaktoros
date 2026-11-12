"""#1349 split of ``gate.py``: ``explain`` and its helpers live in ``diaktoros/explain.py``, and
its single next step in ``diaktoros/explain_next.py`` (split again at the size ceiling, #2261).

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
# The next-step half: its own home, reached as ``explain_next.<name>``, never through ``explain``.
NEXT = ("next_step", "_next_unread", "_next_approval", "_next_person", "_next_review_only",
        "_next_ruling", "redelivery_outcome", "fixer_retry", "_mark_wait", "_started_turn_step",
        "_reconcile_first", "_ended_turn_step", "_ended_turn_recovery", "_declined_step",
        "_next_push_off", "_next_escalation", "_next_lock", "_next_run")
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

    def test_no_other_module_keeps_its_own_copy_of_a_moved_name(self):
        # #1503: scripts/watchdog.py kept a narrower LIVE_TURN of its own, one question answered
        # two ways. The watchdog reads run_supervisor.NOT_ENDED, as explain's live part does.
        for path in (ROOT / "scripts/watchdog.py", *sorted((ROOT / "diaktoros").rglob("*.py"))):
            if path.name == "explain.py" and path.parent.name == "diaktoros":
                continue
            with self.subTest(path.relative_to(ROOT).as_posix()):
                self.assertEqual(sorted(top_level(path) & set(MOVED)), [])

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

    def test_the_next_step_lives_in_explain_next_only_and_is_reached_there(self):
        nxt = top_level(ROOT / "diaktoros/explain_next.py")
        self.assertEqual(sorted(set(NEXT) - nxt), [])
        # Its old names stay out of explain (other modules have a next_step of their own).
        old_names = {n for n in NEXT} | {"_" + n for n in NEXT if not n.startswith("_")}
        self.assertEqual(sorted(top_level(ROOT / "diaktoros/explain.py") & old_names), [])
        names = "|".join(n.lstrip("_") for n in NEXT)
        old = re.compile(rf"\bexplain\._?({names})\b|\(explain, \"_?({names})\"")
        for root in ("diaktoros", "scripts", "tests", "docs"):
            for path in sorted((ROOT / root).rglob("*")):
                if path.suffix not in (".py", ".md") or path == pathlib.Path(__file__):
                    continue
                with self.subTest(path.relative_to(ROOT).as_posix()):
                    self.assertEqual([m.group(0) for m in old.finditer(path.read_text())], [])


if __name__ == "__main__":
    unittest.main()
