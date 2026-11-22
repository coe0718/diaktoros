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


def module_bindings(trees) -> Counter:
    """Every binding of every name anywhere in ``trees``, inside functions too, plus each
    attribute store or ``setattr`` of a literal name (``mod.NAME = …``), which may rebind a
    module's constant from elsewhere (seventh review on #2587)."""
    found = Counter()
    for node in (node for tree in trees for node in ast.walk(tree)):
        found.update(_bound(node))
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            found[node.attr] += 1
        elif (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'setattr'
              and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
            found[str(node.args[1].value)] += 1
    return found


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
