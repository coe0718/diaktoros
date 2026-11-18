"""The observer-event scan that ``tests/test_observer_event_literals.py`` enforces (#2301).

``observer.notify`` logs and drops an event it does not know, so a misspelled event at a new call
site sends nothing. ``scan`` reads every call to ``notify`` on the observer module in the sources
it is given: the event argument, positional (the third) or ``event=``, must be a string literal
in ``observer.EVENTS``. A call whose event is not a literal fails unless ``ALLOWED`` names its
function, by qualified name and checked against that function's own node (#2369, #2373), and
says why; the literals that reach an allowed call are then checked instead. What the scan cannot
read fails (review of #2356; ``tools/observer_notify_ast.py`` reads each file).

It sits here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's scan and not the PR's
(#1973, #2509): inside the test file, every regression passed on the base too (#2536 review F1).
``ALLOWED`` is here because the scan reads it. The regression tests stay in the test module,
which puts the repository root on ``sys.path`` for ``diaktoros.observer``.
"""
from __future__ import annotations

import ast

from diaktoros import observer
from observer_notify_ast import (_event_arg, _event_bindings, _is_notify_ref, _is_observer_notify,
                                 _literals, _module_escapes, _observer_names, _own_params)

EVENT_POS = 2  # notify(loop, st, event, number, ...)

# (file, enclosing function's qualified name: ``Class.method``, ``outer.inner``; #2369) ->
# (why the event is not a literal, how its literals are checked).
# ``("assigned", None)``: every binding of ``event`` in that function must be a string literal
# assignment (``event = "x"``, ``event: str = "x"``, ``event, k = "x", …``) and is checked.
# ``("param", index)``: the function forwards its ``event`` parameter (at ``index``) unchanged;
# every call to it, anywhere scanned, must pass a known literal at that position (or ``event=``).
# Either way the notify call must pass the bare name ``event``.
ALLOWED = {
    ("diaktoros/run_supervisor/notices.py", "OperatorNotices.failure_notice"):
        ("the event (failed or held) depends on the run's state", ("assigned", None)),
    ("diaktoros/fix_hold.py", "_notice"):
        ("a wrapper for the issue hand-off's held and fixing notices", ("param", 2)),
    ("diaktoros/broker/ipc.py", "_issue_notice"):
        ("a wrapper for the issue-side triaged and fixed notices", ("param", 2)),
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _functions(tree):
    """Each node of ``tree`` mapped to ``(qualified name, node)`` of its innermost enclosing
    function (``OperatorNotices.failure_notice``, ``outer.inner``), or ``(None, None)``. The
    node itself, not a name, is what an allowed call is checked against (#2369, #2373): a
    second function of the same name in the file is not the one ``ALLOWED`` reviewed."""
    owner = {}

    def visit(node, inner, prefix):
        for child in ast.iter_child_nodes(node):
            here, below = inner, prefix
            if isinstance(child, _FUNCS):
                here = (prefix + child.name, child)
                below = here[0] + "."
            elif isinstance(child, ast.ClassDef):
                below = prefix + child.name + "."
            owner[child] = here
            visit(child, here, below)
    visit(tree, (None, None), "")
    return owner


def _check_allowed(fn, call, how, pos, where):
    """Problems with an allowed non-literal notify ``call`` in ``fn``: it must pass the bare name
    ``event``, bound only as ``how`` says (see ``ALLOWED``)."""
    sent = _event_arg(call, EVENT_POS)
    if not (isinstance(sent, ast.Name) and sent.id == "event"):
        shown = ast.unparse(sent) if sent is not None else "nothing"
        return [f"{where}: {fn.name} sends {shown!r}, not its `event` variable unchanged"]
    found, params = _event_bindings(fn), _own_params(fn)
    if how == "param":
        positional = fn.args.posonlyargs + fn.args.args
        problems = [] if len(positional) > pos and positional[pos].arg == "event" else [
            f"{where}: {fn.name}'s parameter {pos} is not `event`: fix ALLOWED"]
        return problems + [f"{where}: {fn.name} rebinds its `event` parameter"] * bool(found)
    problems = [f"{where}: {fn.name} takes `event` as a parameter, which cannot be checked"] * \
        any(a.arg == "event" for a in params)
    if not found:
        problems.append(f"{where}: no event literals assigned in {fn.name}: scan is broken")
    return problems + [f"{where}: {fn.name} binds `event` to something other than a literal"
                       for v in found if v is None] + [
        f"{where}: unknown observer event {v!r} assigned in {fn.name}"
        for v in found if v is not None and v not in observer.EVENTS]


def _scan_notify(path, tree, wrappers):
    """Problems with the observer notify calls in one file, and how many there are. An allowed
    wrapper is added to ``wrappers`` (``(file, function) -> event position``)."""
    problems, calls, names, owner = [], 0, _observer_names(tree), _functions(tree)
    called = set()
    for node in ast.walk(tree):
        if not _is_observer_notify(node, names):
            continue
        calls += 1
        called.add(id(node.func))
        where = f"{path}:{node.lineno}"
        try:
            values = _literals(_event_arg(node, EVENT_POS))
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
            continue
        if values is not None:
            problems += [f"{where}: unknown observer event {v!r}"
                         for v in values if v not in observer.EVENTS]
            continue
        func, fn = owner.get(node, (None, None))
        allowed = ALLOWED.get((path, func))
        if allowed is None:
            problems.append(f"{where}: event is not a string literal (in {func}); "
                            "pass a literal or add an ALLOWED entry saying why")
            continue
        how, pos = allowed[1]
        problems += _check_allowed(fn, node, how, pos, where)
        if how == "param":
            wrappers[(path, fn.name)] = pos
    problems += [f"{path}:{node.lineno}: observer notify referenced, not called: its event "
                 "cannot be checked" for node in ast.walk(tree)
                 if _is_notify_ref(node, names) and id(node) not in called]
    problems += [f"{path}:{node.lineno}: the observer module is passed on as "
                 f"{ast.unparse(node)!r}, so its notify calls cannot be found: call "
                 "`observer.notify(...)` on it directly" for node in _module_escapes(tree)]
    return problems, calls


def _wrapper_names(tree, path, wrappers):
    """Local name -> wrapper ``(file, function)`` in this file: the wrapper's own name in its
    file, and any ``from ... import name [as alias]`` of a wrapper's name (any module: it
    over-matches rather than miss one)."""
    local = {name: (where, name) for where, name in wrappers if where == path}
    by_name = {name: (where, name) for where, name in wrappers}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in by_name:
                    local[alias.asname or alias.name] = by_name[alias.name]
    return local


def _wrapper_target(func, local, by_name):
    """The wrapper ``func`` refers to: a local name of one, or ``<anything>.<wrapper name>``."""
    if isinstance(func, ast.Name) and isinstance(func.ctx, ast.Load):
        return local.get(func.id)
    if isinstance(func, ast.Attribute):
        return by_name.get(func.attr)
    return None


def _scan_wrapper_calls(path, tree, wrappers):
    """Problems with the calls to allowed wrappers in one file: each must pass a known literal
    event, and a wrapper may be referenced only to call it."""
    problems, called = [], set()
    local = _wrapper_names(tree, path, wrappers)
    by_name = {name: (where, name) for where, name in wrappers}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _wrapper_target(node.func, local, by_name)
        if target is None:
            continue
        called.add(id(node.func))
        where = f"{path}:{node.lineno}"
        try:
            values = _literals(_event_arg(node, wrappers[target]))
        except ValueError as exc:
            problems.append(f"{where}: {target[1]}: {exc}")
            continue
        if values is None:
            problems.append(f"{where}: {target[1]}'s event is not a string literal")
            continue
        problems += [f"{where}: unknown observer event {v!r}"
                     for v in values if v not in observer.EVENTS]
    problems += [f"{path}:{node.lineno}: wrapper referenced, not called: its event cannot be "
                 "checked" for node in ast.walk(tree)
                 if _wrapper_target(node, local, by_name) and id(node) not in called]
    return problems


def scan(sources):
    """Problems in ``sources`` ({relative path: source text}), and the count of notify calls."""
    problems, calls, wrappers, trees = [], 0, {}, {}
    for path, text in sources.items():
        trees[path] = ast.parse(text)
        found, count = _scan_notify(path, trees[path], wrappers)
        problems, calls = problems + found, calls + count
    for path, tree in trees.items():
        problems += _scan_wrapper_calls(path, tree, wrappers)
    return problems, calls


def _entry_used(source, func):
    """Whether an observer notify call in ``source`` is in the function ``func`` names, by its
    qualified name as ``ALLOWED`` keys it (#2369)."""
    tree = ast.parse(source)
    owner, names = _functions(tree), _observer_names(tree)
    return any(_is_observer_notify(n, names) and owner.get(n)[0] == func for n in ast.walk(tree))
