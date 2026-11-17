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


def top_level(path: pathlib.Path, imports: bool = False) -> set[str]:
    """The names ``path`` binds at module level: ``def``/``class``, plain and annotated
    assignments and, with ``imports``, the names an ``import``/``from ... import`` binds (#1502:
    ``from .explain import explain`` in ``gate`` is a re-export as much as ``explain = ...``).
    Imports are opt-in because elsewhere ``from . import explain`` binding the module is fine."""
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif imports and isinstance(node, ast.Import | ast.ImportFrom):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return names


GATE = ROOT / "diaktoros/gate/__init__.py"
# #1502: the alias forms the first scan missed (it saw only def/class and plain assignments).
# gate is the package ``diaktoros.gate`` now, so ``..explain`` is ``diaktoros/explain.py``.
ALIASES = ("from ..explain import explain", "from ..explain import LIVE_TURN as LIVE_TURN",
           "explain: object = None")
STATIC_GUARD = "test_each_moved_name_is_defined_in_explain_only"
RUNTIME_GUARD = "test_gate_does_not_expose_a_moved_name_at_runtime"


def run_guard(test_name: str, gate_path: pathlib.Path, gate_module) -> list[str]:
    """Run the guard test ``test_name`` of ``OneHome`` itself, with ``GATE`` pointing at
    ``gate_path`` and ``diaktoros.gate`` at ``gate_module``; return the subtests that failed.
    This drives the real guard, so dropping its scan or its assertion turns the alias tests red."""
    import sys
    from unittest import mock
    import diaktoros
    result = unittest.TestResult()
    with mock.patch.object(sys.modules[__name__], "GATE", gate_path), \
            mock.patch.object(diaktoros, "gate", gate_module), \
            mock.patch.dict(sys.modules, {"diaktoros.gate": gate_module}):
        OneHome(test_name).run(result)
    return [str(test) for test, _ in result.failures + result.errors]


def gate_copy(line: str):
    """A copy of the real ``diaktoros.gate`` module with ``line`` run in it as gate's own code,
    so a relative import resolves as it would in gate."""
    import types
    from diaktoros import gate
    module = types.ModuleType(gate.__name__)
    module.__dict__.update(gate.__dict__)
    exec(compile(line, str(GATE), "exec"), module.__dict__)
    return module


class OneHome(unittest.TestCase):
    def test_each_moved_name_is_defined_in_explain_only(self):
        gate, explain = top_level(GATE, imports=True), top_level(ROOT / "diaktoros/explain.py")
        for name in MOVED:
            with self.subTest(name):
                self.assertIn(name, explain)
                self.assertNotIn(name, gate)
        for name in SHARED:                   # the predicates the gates run stay with the gates
            with self.subTest(name):
                self.assertIn(name, gate)
                self.assertNotIn(name, explain)

    def test_gate_does_not_expose_a_moved_name_at_runtime(self):
        # The property itself (#1502): no moved name is reachable as ``gate.<name>``.
        from diaktoros import gate
        missing = object()
        for name in MOVED:
            with self.subTest(name):
                self.assertIs(getattr(gate, name, missing), missing)

    def test_the_static_guard_fails_on_each_alias_in_gate_and_passes_without_one(self):
        # #1502: run the guard itself on gate copies; these aliases once left it green.
        import tempfile
        from diaktoros import gate
        source = GATE.read_text()
        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "__init__.py"
            copy.write_text(source)
            self.assertEqual(run_guard(STATIC_GUARD, copy, gate), [])
            for line in ALIASES:
                with self.subTest(line):
                    copy.write_text(f"{source}\n{line}\n")
                    self.assertNotEqual(run_guard(STATIC_GUARD, copy, gate), [])

    def test_the_runtime_guard_fails_on_each_alias_in_gate_and_passes_without_one(self):
        # #1502: the runtime guard itself, on a gate module that ran each alias.
        self.assertEqual(run_guard(RUNTIME_GUARD, GATE, gate_copy("")), [])
        for line in ALIASES:
            with self.subTest(line):
                self.assertNotEqual(run_guard(RUNTIME_GUARD, GATE, gate_copy(line)), [])

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
