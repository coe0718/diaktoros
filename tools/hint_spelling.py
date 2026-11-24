"""The hand-spelled ``dk fixer-push`` hint scan that ``tests/test_show.py`` enforces (#1480, #2039).

``hand_spelled`` names the lines of strings in a Python source that spell ``dk fixer-push``,
docstrings aside: a hint the operator would type, which must come from
``config.fixer_push_enable_command``.

Besides each string constant, the text of every ``+`` chain and every f-string is joined before
matching (#1480), and so is every ``%``-format of a string and every ``str.join`` of a literal
list or tuple (#2039). A name that the source binds exactly once, by a plain module-level
assignment of a string constant, stands for that string (``string_names``). A format is applied
as Python applies it: an f-string's ``!r``/``!a`` conversion and format spec, and a ``%``
conversion's flags, width and precision (a ``*`` taking an argument of its own), so
``f"{cmd:.2}"`` with ``cmd = "dkjunk"`` reads ``dk`` (#2662 review F4, F5). Any other part that
is not a known string (a placeholder, a call, a name bound elsewhere, a format applied to an
unknown part or with an unknown spec) becomes a marker, which stands in for ``dk`` only right
after ``hermes``: ``f"hermes {cmd} " f"fixer-push"`` counts as hand-spelled, ``cmd + " fixer-push"``
with ``cmd`` unknown or ``"run"`` does not (#2039).

What it cannot see: a ``dk`` made at runtime and not preceded by a literal ``hermes``
(``prefix_from_config() + " fixer-push"``). It is a static heuristic, not an evaluator.

It sits here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's scan and not the PR's
(#1973; #2662 review F3): with the scan inside the test file, every regression passed on the base
too. Its regression tests stay in ``tests/test_show.py``. Standard library only.
"""
from __future__ import annotations

import ast
import re

MARKER = "\x00"
_GAP = f"[\\s{MARKER}]*"
PATTERN = re.compile(f"(?:dk|hermes{_GAP}{MARKER}){_GAP}fixer-push")
# One `%` conversion: key, flags, width, precision, type (`%%` matches with every group empty).
_CONVERSION = re.compile(r"%%|%(\([^)]*\))?([-#0 +]*)(\*|\d+)?(?:\.(\*|\d*))?([a-zA-Z])")
_F_CONVERSIONS = {-1: str, ord("s"): str, ord("r"): repr, ord("a"): ascii}


def string_names(tree) -> dict[str, str]:
    """The names ``tree`` binds exactly once, by a plain module-level ``name = "<string>"``,
    each with its string: only these can be read as that string everywhere (#2662 review).

    Every other binding of a name counts against it, so a name also bound as a parameter, by
    ``+=``, ``for``, ``with``, ``except``, ``import``, a walrus, a ``match`` capture, ``del``,
    or a declaration ``global``/``nonlocal``, is unknown and stays a marker: it may hold
    ``dk`` where it is read."""
    bindings: dict[str, int] = {}

    def bind(name: str) -> None:
        bindings[name] = bindings.get(name, 0) + 1
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bind(node.id)
        elif isinstance(node, ast.arg):
            bind(node.arg)
        elif isinstance(node, ast.alias):
            bind((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                bind(name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bind(node.name)
    return {node.targets[0].id: node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and bindings[node.targets[0].id] == 1
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}


def _is_str(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_format(node) -> bool:
    """Whether ``node`` is ``<string> % <args>``."""
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and built(node.left)


def _is_join(node) -> bool:
    """Whether ``node`` is ``<string>.join(<one argument>)``."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and built(node.func.value)
            and len(node.args) == 1 and not node.keywords)


def built(node) -> bool:
    """Whether ``node`` builds a string: a constant, an f-string, a ``+`` chain, a ``%``-format
    or a ``str.join``."""
    return (_is_str(node) or isinstance(node, ast.JoinedStr)
            or isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
            or _is_format(node) or _is_join(node))


def _f_value(node, names: dict[str, str]) -> str:
    """The text of one f-string placeholder, its conversion and format spec applied as Python
    applies them; the marker when the value or the spec is not known (#2662 review F4)."""
    value = text(node.value, names)
    if node.conversion in (-1, ord("s")) and node.format_spec is None:
        return value                    # str() of a string: the text itself, markers kept
    spec = text(node.format_spec, names) if node.format_spec is not None else ""
    convert = _F_CONVERSIONS.get(node.conversion)
    if MARKER in value or MARKER in spec or convert is None:
        return MARKER
    try:
        return format(convert(value), spec)
    except (ValueError, TypeError, OverflowError):
        return MARKER


def _conversion(match, args: list, names: dict[str, str]) -> str:
    """The text one ``%`` conversion formats, taking its arguments off ``args``: a ``*`` width or
    precision takes one of its own before the value (#2662 review F2), and flags, width and
    precision are applied as ``%`` applies them (F5). The marker where that is not known."""
    if match.group(0) == "%%":
        return "%"
    key, flags, width, precision, kind = match.groups()
    if key:                             # %(key)s: the argument is a mapping
        return MARKER
    stars = []
    for _ in range((width == "*") + (precision == "*")):
        if not args:
            return MARKER
        star = args.pop(0)
        stars.append(star.value if isinstance(star, ast.Constant)
                     and type(star.value) is int else None)
    if not args:
        return MARKER
    arg = args.pop(0)
    value = text(arg, names)
    if kind == "s" and not flags and width is None and precision is None:
        return value                    # a plain %s: the text itself, markers kept
    if MARKER not in value:
        pass                            # a known string
    elif isinstance(arg, ast.Constant) and not isinstance(arg.value, str):
        value = arg.value               # a number or other literal, formatted as itself
    else:
        return MARKER
    if None in stars:
        return MARKER
    spec = "%" + flags + (width or "") + ("." + precision if precision is not None else "") + kind
    try:
        return spec % (*stars, value)
    except (ValueError, TypeError, OverflowError):
        return MARKER


def _formatted(node, names: dict[str, str]) -> str:
    """The text of ``<string> % <args>``, each conversion replaced by what it formats."""
    args = list(node.right.elts) if isinstance(node.right, ast.Tuple) else [node.right]
    return _CONVERSION.sub(lambda match: _conversion(match, args, names), text(node.left, names))


def text(node, names: dict[str, str]) -> str:
    """The string ``node`` builds, with a marker for each part not known here."""
    if _is_str(node):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.JoinedStr):
        return "".join(text(part, names) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _f_value(node, names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return text(node.left, names) + text(node.right, names)
    if _is_format(node):
        return _formatted(node, names)
    if _is_join(node) and isinstance(node.args[0], ast.List | ast.Tuple):
        return text(node.func.value, names).join(text(item, names) for item in node.args[0].elts)
    return MARKER


def hand_spelled(source: str) -> list[int]:
    """Lines of strings in ``source`` naming ``dk fixer-push``, docstrings aside (module
    docstring: what is joined, what is applied, where the marker counts)."""
    tree = ast.parse(source)
    names = string_names(tree)
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body and isinstance(node.body[0], ast.Expr)}
    return sorted({node.lineno for node in ast.walk(tree)
                   if built(node) and id(node) not in docs and PATTERN.search(text(node, names))})
