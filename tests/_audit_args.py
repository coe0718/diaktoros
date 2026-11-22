"""What an audit call passes for a parameter (``_audit_writers``): by position, by keyword, or
inside a ``**`` expansion the guard can read. Anything it cannot read fails closed (third
review on #2587), so a writer can never record a role the vocabulary check did not see.
"""
from __future__ import annotations

import ast


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


def bindings(name, nodes) -> int:
    """How many of ``nodes`` (a function's own) bind ``name``: each store or ``del`` of it
    (``+=``, a loop or ``with`` target, unpacking as much as ``name = …``), an import, ``except``
    or ``match`` capture as it, a nested ``def`` or ``class`` of it; a ``global`` or ``nonlocal``
    counts as unbounded. The resolver reads only a plain ``name = …``, so any other binding fails
    it closed (fifth and sixth reviews on #2587)."""
    count = 0
    for node in nodes:
        if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load):
            count += 1
        elif isinstance(node, ast.alias) and (node.asname or node.name.partition('.')[0]) == name:
            count += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            count += 1
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            count += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            count += 1
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            count += 1
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            count += 1_000
    return count


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
