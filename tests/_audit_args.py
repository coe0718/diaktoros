"""What an audit call passes for a parameter (``_audit_writers``): by position, by keyword, or
inside a ``**`` expansion the guard can read. Anything it cannot read fails closed (third
review on #2587), so a writer can never record a role the vocabulary check did not see.
"""
from __future__ import annotations

import ast
from collections import Counter


class Unresolved(AssertionError):
    pass


def _expanded(node, name, where):
    """``name``'s value in a ``**`` expansion: a ``{...}`` literal or a ``dict(...)`` call with
    literal keys; ``None`` when it provably has none. Anything else could hold it (third review
    on #2587), so it fails closed. A repeated literal key keeps its last value, as Python does
    (fourth review on #2587)."""
    if isinstance(node, ast.Dict) and all(isinstance(k, ast.Constant) for k in node.keys):
        return ([None] + [v for k, v in zip(node.keys, node.values) if k.value == name])[-1]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'dict'
            and not node.args and all(kw.arg is not None for kw in node.keywords)):
        return next((kw.value for kw in node.keywords if kw.arg == name), None)
    raise Unresolved(where)


def _bound(node) -> list[str]:
    """The names ``node`` binds: a store or ``del`` of a name (``+=``, a loop or ``with`` target,
    unpacking as much as ``name = …``), an import, an ``except`` or ``match`` capture, a ``def``
    or ``class``; a ``global`` or ``nonlocal`` names each of its names a thousand times
    (unbounded)."""
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return [node.id]
    if isinstance(node, ast.alias):
        return [node.asname or node.name.partition('.')[0]]
    if isinstance(node, (ast.MatchAs, ast.MatchStar, ast.FunctionDef, ast.AsyncFunctionDef,
                         ast.ClassDef, ast.ExceptHandler)) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names) * 1_000
    return []


def bindings(name, nodes) -> int:
    """How many of ``nodes`` (a function's own) bind ``name`` (``_bound``). The resolver reads
    only a plain ``name = …``, so any other binding fails it closed (fifth and sixth reviews on
    #2587)."""
    return sum(_bound(node).count(name) for node in nodes)


# A name's namespace dict (eighth review on #2587): ``globals()['NAME'] = …`` rebinds NAME with
# no name or attribute store. Read only through these; any other use may write to it.
_NAMESPACES, _READS = {'globals', 'locals', 'vars'}, {'get', 'keys', 'values', 'items', 'copy'}
ANY = '*'                     # a write the guard cannot name: every constant fails closed


def _namespace(node) -> bool:
    return ((isinstance(node, ast.Call) and getattr(node.func, 'id', '') in _NAMESPACES)
            or (isinstance(node, ast.Attribute) and node.attr == '__dict__'))


def _namespace_writes(node, up) -> str | None:
    """The name a namespace dict ``node`` is written under (``ANY`` when it may be written
    under any name), or None when it is only read: a load of a key, or a read method."""
    parent = up.get(node)
    if isinstance(parent, ast.Subscript) and parent.value is node:
        if isinstance(parent.ctx, ast.Load):
            return None
        key = parent.slice
        return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else ANY
    if (isinstance(parent, ast.Attribute) and parent.attr in _READS
            and isinstance(up.get(parent), ast.Call) and up[parent].func is parent):
        return None
    return ANY                        # passed on, aliased, updated, assigned: may write any name


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)


def _own(scope):
    """The nodes in ``scope`` itself: its parameters and body, not a nested scope's."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, _SCOPES):
            todo.extend(ast.iter_child_nodes(node))


def _never_module(target, up, modules) -> bool:
    """Whether ``target`` is a name bound, in the scope it resolves in, only to what is never a
    scanned module (ninth review on #2587): an ``except … as`` exception, or ``import pkg`` (as
    any name) of a package outside ``modules``. Any other binding (an assignment, a parameter, an
    alias of ``sys.modules[…]``) might be one; a parameter shadowing a safe import may be passed a
    module (tenth). ``from pkg import name`` or ``import pkg.sub as name`` binds an attribute of
    that package, which it may have set to a scanned module (eleventh), so it is not shown safe.
    A class body binding is read with the scope outside it, failing closed."""
    if not isinstance(target, ast.Name):
        return False
    seen, scope = set(), up.get(target)
    while scope is not None:
        if isinstance(scope, _SCOPES):
            for node in _own(scope):
                imported = up.get(node)
                safe = isinstance(node, ast.ExceptHandler) or (
                    isinstance(node, ast.alias) and isinstance(imported, ast.Import)
                    and not (node.asname and '.' in node.name)
                    and node.name.partition('.')[0] not in modules)
                named = _bound(node) + ([node.arg] if isinstance(node, ast.arg) else [])
                seen |= {safe} if target.id in named else set()
            if seen and not isinstance(scope, ast.ClassDef):
                break
        scope = up.get(scope)
    return seen == {True}


def module_bindings(trees, modules=frozenset()) -> Counter:
    """Every binding of every name anywhere in ``trees``, inside functions too, plus each
    attribute store or ``setattr`` of a literal name (``mod.NAME = …``), which may rebind a
    module's constant from elsewhere (seventh review on #2587), and each write to a namespace
    dict (eighth). A write whose name the guard cannot read (``globals().update(…)``, a
    ``setattr`` of a computed name on anything not shown never to be a scanned module: ninth)
    counts under ``ANY``."""
    found = Counter()
    for tree in trees:
        up = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            found.update(_bound(node))
            if _namespace(node) and (name := _namespace_writes(node, up)) is not None:
                found[name] += 1
            elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
                found[node.attr] += 1
            elif (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'setattr'
                  and len(node.args) > 1):
                if isinstance(node.args[1], ast.Constant):
                    found[str(node.args[1].value)] += 1
                elif not _never_module(node.args[0], up, modules):
                    found[ANY] += 1
    return found


def constant(bound: Counter, name: str) -> bool:
    """Whether ``name`` is bound once, and nothing writes a name the guard cannot read."""
    return bound[name] == 1 and not bound[ANY]


def passed(call, index, name, where):
    """The node ``call`` passes for parameter ``name`` (at ``index``, or keyword-only when
    ``None``): an explicit keyword, a positional argument, or a key of a ``**`` expansion it can
    read. ``None`` when the call passes nothing for it; ``Unresolved`` when a ``*`` or ``**``
    expansion might, or when it passes the parameter twice (a call Python refuses)."""
    found = [kw.value for kw in call.keywords if kw.arg == name]
    for kw in call.keywords:
        if kw.arg is None and (value := _expanded(kw.value, name, where)) is not None:
            found.append(value)
    if index is not None:
        for position, arg in enumerate(call.args):
            if isinstance(arg, ast.Starred):
                raise Unresolved(where)
            if position == index:
                found.append(arg)
    if len(found) > 1:
        raise Unresolved(where)
    return found[0] if found else None
