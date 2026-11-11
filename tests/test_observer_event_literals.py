"""Every ``observer.notify`` call names an event in ``observer.EVENTS`` (#2301).

``observer.notify`` logs and drops an event it does not know, so a misspelled event at a new call
site sends nothing. This scans every ``observer.notify(...)`` call in ``diaktoros/`` and
``scripts/`` (in the style of ``tests/test_state_junk.py::ExplainKinds``): the event argument,
positional (the third) or ``event=``, must be a string literal in ``observer.EVENTS``. A call
whose event is not a literal fails unless ``ALLOWED`` names its function and says why; the
literals that reach an allowed call are then checked instead (see ``ALLOWED``).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from diaktoros import observer  # noqa: E402

EVENT_POS = 2  # notify(loop, st, event, number, ...)

# (file, enclosing function) -> (why the event is not a literal, how its literals are checked).
# ``("assigned", None)``: every string assigned to ``event`` in that function is checked.
# ``("param", index)``: the function forwards its ``event`` parameter; every call to it by name,
# anywhere scanned, must pass a known literal at that position (or ``event=``).
ALLOWED = {
    ("diaktoros/run_supervisor/notices.py", "failure_notice"):
        ("the event (failed or held) depends on the run's state", ("assigned", None)),
    ("diaktoros/fix_hold.py", "_notice"):
        ("a wrapper for the issue hand-off's held and fixing notices", ("param", 2)),
    ("diaktoros/broker_ipc.py", "_issue_notice"):
        ("a wrapper for the issue-side triaged and fixed notices", ("param", 2)),
}


def _is_observer_notify(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "notify" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "observer")


def _event_arg(call, pos):
    """The event argument of ``call``: positional at ``pos`` or ``event=``; ``None`` if absent."""
    if len(call.args) > pos:
        return call.args[pos]
    for kw in call.keywords:
        if kw.arg == "event":
            return kw.value
    return None


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literals(node):
    """The event strings ``node`` can be: a literal, or ``"a" if c else "b"`` with literal
    branches (``scripts/gate_reviewer.py``'s opened/handoff notice); ``None`` for anything else."""
    if isinstance(node, ast.IfExp):
        body, orelse = _literals(node.body), _literals(node.orelse)
        return body + orelse if body is not None and orelse is not None else None
    value = _literal(node) if node is not None else None
    return None if value is None else [value]


def _functions(tree):
    """Each node of ``tree`` mapped to the name of its innermost enclosing function."""
    owner = {}

    def visit(node, name):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                else name
            owner[child] = inner
            visit(child, inner)
    visit(tree, None)
    return owner


def _assigned_events(fn):
    """The string literals ``fn`` assigns to ``event``: ``event = "x"`` or ``event, k = "x", …``.
    Any other assignment to ``event`` yields ``None``, which the scan reports as unknown."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "event":
                found.append(_literal(node.value))
            elif isinstance(target, ast.Tuple):
                for i, elt in enumerate(target.elts):
                    if isinstance(elt, ast.Name) and elt.id == "event":
                        value = node.value
                        found.append(_literal(value.elts[i]) if isinstance(value, ast.Tuple)
                                     and len(value.elts) == len(target.elts) else None)
    return found


def scan(sources):
    """Problems in ``sources`` ({relative path: source text}), and the count of notify calls."""
    problems, calls, wrappers, trees = [], 0, {}, {}
    for path, text in sources.items():
        tree = trees[path] = ast.parse(text)
        owner = _functions(tree)
        for node in ast.walk(tree):
            if not _is_observer_notify(node):
                continue
            calls += 1
            where = f"{path}:{node.lineno}"
            values = _literals(_event_arg(node, EVENT_POS))
            if values is not None:
                problems += [f"{where}: unknown observer event {v!r}"
                             for v in values if v not in observer.EVENTS]
                continue
            func = owner.get(node)
            allowed = ALLOWED.get((path, func))
            if allowed is None:
                problems.append(f"{where}: event is not a string literal (in {func}); "
                                "pass a literal or add an ALLOWED entry saying why")
                continue
            how, pos = allowed[1]
            if how == "param":
                wrappers[(path, func)] = pos
                continue
            fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,
                      ast.AsyncFunctionDef)) and n.name == func)
            found = _assigned_events(fn)
            if not found:
                problems.append(f"{where}: no event literals assigned in {func}: scan is broken")
            problems += [f"{where}: unknown observer event {v!r} assigned in {func}"
                         for v in found if v not in observer.EVENTS]
    for path, tree in trees.items():
        for node in ast.walk(tree):
            target = _wrapper_called(node, path, wrappers)
            if target is None:
                continue
            values = _literals(_event_arg(node, wrappers[target]))
            if values is None:
                problems.append(f"{path}:{node.lineno}: {target[1]}'s event is not a string "
                                "literal")
                continue
            problems += [f"{path}:{node.lineno}: unknown observer event {v!r}"
                         for v in values if v not in observer.EVENTS]
    return problems, calls


def _wrapper_called(node, path, wrappers):
    """The ``(file, function)`` of the allowed wrapper ``node`` calls, or ``None``: by bare name
    within the wrapper's own file, or as ``module.name`` from anywhere."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    for where, name in wrappers:
        if isinstance(func, ast.Name) and func.id == name and where == path:
            return where, name
        if (isinstance(func, ast.Attribute) and func.attr == name
                and isinstance(func.value, ast.Name)
                and func.value.id == pathlib.PurePosixPath(where).stem):
            return where, name
    return None


def repo_sources():
    files = sorted((ROOT / "diaktoros").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    return {p.relative_to(ROOT).as_posix(): p.read_text() for p in files}


class ObserverEventLiterals(unittest.TestCase):
    def test_every_notify_event_is_declared(self):
        problems, calls = scan(repo_sources())
        self.assertGreater(calls, 20, "few observer.notify calls found: the scan is broken")
        self.assertEqual(problems, [])

    def test_allowlist_entries_are_used(self):
        # A stale entry would silently allow a non-literal event in a future function.
        sources = repo_sources()
        for path, func in ALLOWED:
            tree = ast.parse(sources[path])
            owner = _functions(tree)
            self.assertTrue(any(_is_observer_notify(n) and owner.get(n) == func
                                for n in ast.walk(tree)), f"stale ALLOWED entry {path}:{func}")

    def test_planted_unknown_event_fails(self):
        planted = "def f(loop, st):\n    observer.notify(loop, st, 'no-such-event', 1, '')\n"
        problems, _ = scan({"diaktoros/planted.py": planted})
        self.assertEqual(len(problems), 1)
        self.assertIn("'no-such-event'", problems[0])

    def test_planted_keyword_unknown_event_fails(self):
        planted = "observer.notify(loop, st, event='no-such-event', number=1)\n"
        self.assertEqual(len(scan({"scripts/planted.py": planted})[0]), 1)

    def test_planted_non_literal_event_fails(self):
        planted = "def g(loop, st, kind):\n    observer.notify(loop, st, kind, 1, '')\n"
        problems, _ = scan({"diaktoros/planted.py": planted})
        self.assertEqual(len(problems), 1)
        self.assertIn("not a string literal", problems[0])

    def test_planted_wrapper_caller_unknown_event_fails(self):
        sources = repo_sources()
        sources["diaktoros/planted.py"] = (
            "def h(loop, scope):\n    broker_ipc._issue_notice(loop, scope, 'trigaed', 'x')\n")
        problems, _ = scan(sources)
        self.assertEqual(problems, ["diaktoros/planted.py:2: unknown observer event 'trigaed'"])


if __name__ == "__main__":
    unittest.main()
