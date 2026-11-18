"""How the observer-event scan (``tools/observer_event_literals.py``) reads one file's AST.

Which names reach the observer module and its ``notify`` (an alias, ``from .observer import
notify as send``, ``dk.observer`` through a package alias) and where the module escapes as a
value; the event argument of a call, failing on a ``*`` or ``**`` argument that could carry it;
the literals an event expression can be; and what a function binds ``event`` to, every binding
the scan cannot read counted as unknown (review of #2356). Nothing here knows the events.

It sits here, outside ``tests/``, with the scan, so the host's base-failure proof (``change_proof``,
which copies a PR's test paths onto the base) runs a PR's tests against the base's rules and not
the PR's (#1973, #2509): inside the test file, every regression passed on the base too (#2536
review F1). The regression tests stay in ``tests/test_observer_event_literals.py``. Standard
library only.
"""
from __future__ import annotations

import ast

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
