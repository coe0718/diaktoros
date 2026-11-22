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
    on #2587), so it fails closed."""
    if isinstance(node, ast.Dict) and all(isinstance(k, ast.Constant) for k in node.keys):
        return next((v for k, v in zip(node.keys, node.values) if k.value == name), None)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'dict'
            and not node.args and all(kw.arg is not None for kw in node.keywords)):
        return next((kw.value for kw in node.keywords if kw.arg == name), None)
    raise Unresolved(where)


def passed(call, index, name, where):
    """The node ``call`` passes for parameter ``name`` (at ``index``, or keyword-only when
    ``None``): an explicit keyword, a positional argument, or a key of a ``**`` expansion it can
    read. ``None`` when the call passes nothing for it; ``Unresolved`` when a ``*`` or ``**``
    expansion might."""
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
    return found[0] if found else None
