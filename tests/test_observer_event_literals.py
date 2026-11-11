"""Every ``observer.notify`` call names an event in ``observer.EVENTS`` (#2301).

``observer.notify`` logs and drops an event it does not know, so a misspelled event at a new call
site sends nothing. This scans every call to ``notify`` on the observer module in ``diaktoros/``
and ``scripts/`` (in the style of ``tests/test_state_junk.py::ExplainKinds``): the event argument,
positional (the third) or ``event=``, must be a string literal in ``observer.EVENTS``. A call
whose event is not a literal fails unless ``ALLOWED`` names its function and says why; the
literals that reach an allowed call are then checked instead (see ``ALLOWED``).

The scan fails closed on what it cannot read (review of #2356): the observer module or its
``notify`` imported under another name, or reached through a package alias (``dk.observer``),
is followed, and the module passed on as a value (``obs = observer``) fails; a starred or ``**``
argument that could carry the event fails; an allowed function must pass its ``event`` variable
unchanged, bound only in the forms the scan reads (any other binding, ``def event`` included,
fails); an allowed wrapper's callers are found by name, by ``module.name`` and
through ``from ... import`` (aliases too), and any other reference to the wrapper fails.
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
# ``("assigned", None)``: every binding of ``event`` in that function must be a string literal
# assignment (``event = "x"``, ``event: str = "x"``, ``event, k = "x", …``) and is checked.
# ``("param", index)``: the function forwards its ``event`` parameter (at ``index``) unchanged;
# every call to it, anywhere scanned, must pass a known literal at that position (or ``event=``).
# Either way the notify call must pass the bare name ``event``.
ALLOWED = {
    ("diaktoros/run_supervisor/notices.py", "failure_notice"):
        ("the event (failed or held) depends on the run's state", ("assigned", None)),
    ("diaktoros/fix_hold.py", "_notice"):
        ("a wrapper for the issue hand-off's held and fixing notices", ("param", 2)),
    ("diaktoros/broker_ipc.py", "_issue_notice"):
        ("a wrapper for the issue-side triaged and fixed notices", ("param", 2)),
}

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _observer_imports(tree):
    """``(modules, functions)``: the local names an import in ``tree`` binds to the observer
    module (``from . import observer as obs``, ``import diaktoros.observer as obs``) and to its
    ``notify`` (``from .observer import notify as send``). File-wide, so it over-matches."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "observer":
                    modules.add(alias.asname or alias.name)
                elif module.split(".")[-1] == "observer" and alias.name in ("notify", "*"):
                    functions.add(alias.asname or "notify")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "observer" and alias.asname:
                    modules.add(alias.asname)
    return modules, functions


def _observer_names(tree):
    """``(modules, functions)`` as ``_observer_imports`` reads them, with ``observer`` always a
    module name (a call ``observer.notify`` counts however the name got there)."""
    modules, functions = _observer_imports(tree)
    return modules | {"observer"}, functions


def _dotted(node):
    """``a.b.c`` for a chain of names and attributes, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def _is_notify_ref(node, names):
    """``node`` names the observer's ``notify``: ``<module alias>.notify``,
    ``<anything>.observer.notify`` (``diaktoros.observer.notify``, or ``dk.observer.notify``
    after ``import diaktoros as dk``) or a name ``notify`` was imported as."""
    modules, functions = names
    if isinstance(node, ast.Name):
        return node.id in functions
    if isinstance(node, ast.Attribute) and node.attr == "notify":
        owner = node.value
        return _dotted(owner) in modules or (
            isinstance(owner, ast.Attribute) and owner.attr == "observer")
    return False


def _module_escapes(tree):
    """The nodes in ``tree`` that hand the observer module itself on instead of reading an
    attribute of it (``obs = observer``, ``f(observer)``, ``getattr(dk.observer, "notify")``).
    Through such a name a notify call cannot be told from any other, so the scan fails on each.
    The module is ``<anything>.observer`` or a name an import binds to it."""
    imported, _ = _observer_imports(tree)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    escapes = []
    for node in ast.walk(tree):
        is_module = ((isinstance(node, ast.Name) and node.id in imported)
                     or (isinstance(node, ast.Attribute) and node.attr == "observer"))
        if not is_module or not isinstance(node.ctx, ast.Load):
            continue
        up = parent.get(node)
        if not (isinstance(up, ast.Attribute) and up.value is node):
            escapes.append(node)
    return escapes


def _is_observer_notify(node, names=({"observer"}, set())):
    return isinstance(node, ast.Call) and _is_notify_ref(node.func, names)


def _event_arg(call, pos):
    """The event argument of ``call``: positional at ``pos`` or ``event=``; ``None`` if absent.
    Raises ``ValueError`` when a ``*`` or ``**`` argument could be the event, which the scan
    cannot read (``notify(*(loop, st), "x", …)`` sends ``"x"``, not the fourth argument)."""
    if any(isinstance(arg, ast.Starred) for arg in call.args[:pos + 1]):
        raise ValueError("a starred argument could carry the event")
    if len(call.args) > pos:
        return call.args[pos]
    for kw in call.keywords:
        if kw.arg == "event":
            return kw.value
    if any(kw.arg is None for kw in call.keywords):
        raise ValueError("a ** argument could carry the event")
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
            inner = child.name if isinstance(child, _FUNCS) else name
            owner[child] = inner
            visit(child, inner)
    visit(tree, None)
    return owner


def _bound(target, value, found, read):
    """Record what assigning ``value`` to ``target`` binds ``event`` to: the literal, or
    ``None`` when it is not one (or the unpacking cannot be matched up)."""
    if isinstance(target, ast.Name):
        if target.id == "event":
            found.append(_literal(value))
            read.add(id(target))
    elif isinstance(target, (ast.Tuple, ast.List)):
        pairs = (isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                 and not any(isinstance(e, ast.Starred) for e in target.elts + value.elts))
        for i, elt in enumerate(target.elts):
            _bound(elt, value.elts[i] if pairs else None, found, read)


# String fields that never bind a name: an attribute, a call's ``name=``, a constant, a
# ``from`` module path. Every other string field of a node is treated as a possible binding.
_NOT_BINDINGS = {(ast.Attribute, "attr"), (ast.keyword, "arg"), (ast.Constant, "value"),
                 (ast.Constant, "kind"), (ast.ImportFrom, "module")}


def _names_bound(node):
    """The names ``node`` could bind. Read generically from its string fields, so a form not
    listed here (``def event``, ``class event``, a ``match`` capture, an ``except ... as``, a
    nested parameter, or a syntax added later) still counts; a ``Name`` only when stored or
    deleted, an import by the name it binds, and ``global``/``nonlocal`` by their lists."""
    if isinstance(node, ast.Name):
        return [] if isinstance(node.ctx, ast.Load) else [node.id]
    if isinstance(node, ast.alias):
        return [node.asname or node.name.split(".")[0]]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    return [value for field, value in ast.iter_fields(node)
            if isinstance(value, str) and (type(node), field) not in _NOT_BINDINGS]


def _event_bindings(fn):
    """What ``fn``'s body binds ``event`` to, one entry per binding: the literal, or ``None`` for
    a binding the scan cannot read: a non-literal value, ``+=``, a ``for``/``with``/``except``/
    walrus target, an import, a nested parameter, ``def``/``class event`` (a decorator can make
    it any value), ``global``/``nonlocal``, and anything else ``_names_bound`` finds. ``fn``'s
    own parameters are not included (see ``_own_params``)."""
    found, read = [], {id(fn)} | {id(a) for a in _own_params(fn)}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _bound(target, node.value, found, read)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _bound(node.target, node.value, found, read)
        elif isinstance(node, ast.AnnAssign):
            read.add(id(node.target))    # ``event: str`` alone binds nothing at run time
    found += [None for node in ast.walk(fn)
              if id(node) not in read and "event" in _names_bound(node)]
    return found


def _own_params(fn):
    """``fn``'s parameters, positional ones first in order."""
    args = fn.args
    rest = [a for a in (args.vararg, args.kwarg) if a is not None]
    return args.posonlyargs + args.args + args.kwonlyargs + rest


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
        func = owner.get(node)
        allowed = ALLOWED.get((path, func))
        if allowed is None:
            problems.append(f"{where}: event is not a string literal (in {func}); "
                            "pass a literal or add an ALLOWED entry saying why")
            continue
        how, pos = allowed[1]
        fn = next(n for n in ast.walk(tree) if isinstance(n, _FUNCS) and n.name == func)
        problems += _check_allowed(fn, node, how, pos, where)
        if how == "param":
            wrappers[(path, func)] = pos
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


def repo_sources():
    files = sorted((ROOT / "diaktoros").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    return {p.relative_to(ROOT).as_posix(): p.read_text() for p in files}


def edited(path, old, new):
    """The repo's sources with ``old`` replaced once by ``new`` in ``path``."""
    sources = repo_sources()
    assert sources[path].count(old) == 1, f"{old!r} not found once in {path}"
    sources[path] = sources[path].replace(old, new)
    return sources


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
            owner, names = _functions(tree), _observer_names(tree)
            self.assertTrue(any(_is_observer_notify(n, names) and owner.get(n) == func
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


class ScanBypasses(unittest.TestCase):
    """Review of #2356: each of these sent an unchecked event and the scan passed."""

    def assertFlags(self, sources, text):
        problems, _ = scan(sources)
        self.assertTrue(problems, "the planted bypass passed the scan")
        self.assertTrue(any(text in p for p in problems), problems)

    def test_annotated_reassignment_in_assigned_function_fails(self):
        # F1: ``event: str = "..."`` after ``event = "failed"`` in failure_notice.
        self.assertFlags(edited("diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                                'event = "failed"\n            event: str = "no-such-event"\n'),
                         "'no-such-event'")

    def test_other_rebinding_in_assigned_function_fails(self):
        # F1: any binding the scan cannot read (here ``+=``) fails rather than being skipped.
        self.assertFlags(edited("diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                                'event = "failed"\n            event += "-typo"\n'),
                         "other than a literal")

    def test_wrapper_forwarding_an_expression_fails(self):
        # F1: the wrapper sends ``event + "-typo"``, not the parameter its callers are checked on.
        old = "observer.notify(loop, state_mod.state_for(loop), event, scope.number"
        self.assertFlags(edited("diaktoros/broker_ipc.py", old, old.replace(
            ", event,", ', event + "-typo",')), "not its `event` variable unchanged")

    def test_wrapper_rebinding_its_parameter_fails(self):
        old = "    from . import observer, state as state_mod\n    try:\n        observer.notify("
        self.assertFlags(edited("diaktoros/broker_ipc.py", old, old.replace(
            "    try:\n", '    event = "no-such-event"\n    try:\n')), "rebinds its `event`")

    def test_wrapper_imported_by_name_fails(self):
        # F2: ``from .broker_ipc import _issue_notice`` (and an alias) in another module.
        for imported, called in (("_issue_notice", "_issue_notice"),
                                 ("_issue_notice as note", "note")):
            sources = repo_sources()
            sources["diaktoros/planted.py"] = (
                f"from .broker_ipc import {imported}\n\n\ndef h(loop, scope):\n"
                f"    {called}(loop, scope, 'no-such-event', 'outcome')\n")
            self.assertFlags(sources, "diaktoros/planted.py:5: unknown observer event")

    def test_wrapper_referenced_without_a_call_fails(self):
        sources = repo_sources()
        sources["diaktoros/planted.py"] = (
            "from . import broker_ipc\nsend = broker_ipc._issue_notice\n")
        self.assertFlags(sources, "wrapper referenced, not called")

    def test_starred_arguments_fail(self):
        # F3: at run time the third argument is 'no-such-event', not the fourth 'held'.
        for call in ("observer.notify(*(loop, st), 'no-such-event', 'held')",
                     "observer.notify(loop, st, **kw)"):
            self.assertFlags({"diaktoros/planted.py": f"def f(loop, st, kw):\n    {call}\n"},
                             "argument could carry the event")

    def test_observer_module_alias_fails(self):
        # F4: the observer module, or its notify, under another name.
        for header, call in (("from . import observer as obs", "obs.notify"),
                             ("import diaktoros.observer as obs", "obs.notify"),
                             ("from .observer import notify as send", "send"),
                             ("import diaktoros.observer", "diaktoros.observer.notify")):
            planted = f"{header}\n\n\ndef f(loop, st):\n    {call}(loop, st, 'no-such-event', 1)\n"
            self.assertFlags({"diaktoros/planted.py": planted},
                             "diaktoros/planted.py:5: unknown observer event 'no-such-event'")

    def test_notify_referenced_without_a_call_fails(self):
        planted = "from . import observer as obs\nsend = obs.notify\n"
        self.assertFlags({"diaktoros/planted.py": planted}, "referenced, not called")

    def test_decorated_definition_rebinding_event_fails(self):
        # Review 2 of #2356: a decorator rebinds ``event`` to any value, here an unknown one.
        for definition in ("def event():\n                pass\n",
                           "class event:\n                pass\n"):
            self.assertFlags(edited(
                "diaktoros/run_supervisor/notices.py", 'event = "failed"\n',
                'event = "failed"\n            @(lambda fn: "no-such-event")\n'
                f'            {definition}'), "other than a literal")

    def test_observer_reached_through_a_package_alias_fails(self):
        # Review 2 of #2356: ``import diaktoros as dk`` then ``dk.observer.notify(...)``.
        planted = ("import diaktoros as dk\n\n\ndef f(loop, st):\n"
                   "    dk.observer.notify(loop, st, 'no-such-event', 1)\n")
        self.assertFlags({"diaktoros/planted.py": planted},
                         "diaktoros/planted.py:5: unknown observer event 'no-such-event'")

    def test_observer_module_passed_on_fails(self):
        # Review 2 of #2356: ``obs = observer; obs.notify(...)`` and the like hide the call.
        for header, line in (("from . import observer", "obs = observer"),
                             ("import diaktoros as dk", "obs = dk.observer"),
                             ("from . import observer as o", "send = getattr(o, 'notify')"),
                             ("from . import observer", "relay(observer)")):
            planted = (f"{header}\n\n\ndef f(loop, st):\n    {line}\n"
                       "    obs.notify(loop, st, 'no-such-event', 1)\n")
            self.assertFlags({"diaktoros/planted.py": planted},
                             "diaktoros/planted.py:5: the observer module is passed on")


if __name__ == "__main__":
    unittest.main()
