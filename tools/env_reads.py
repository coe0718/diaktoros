"""The env-read scan that ``tests/test_env_reads_via_envnames.py`` enforces (#2300).

#542 was a variable read by its new name only, which missed installs still on the old
``REVIEW_LOOP_`` spelling; ``diaktoros/envnames.py`` reads both. ``direct_reads`` finds each
direct ``os.environ.get(...)``, ``os.environ[...]`` (a read or an augmented assignment) or
``os.getenv(...)``, positional or by keyword, whose key is a literal with either prefix, an
``envnames.name(...)`` value or a name bound to one, in code and in Python held in a string (a
generated child-process script, read with ``tools/env_read_scripts.py``). ``scan`` counts them by
file, variable and kind over the files it is given, ``envnames.py`` itself left out. The
allowlist of reads that must stay direct is the test's.

It sits here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's scan and not the PR's
(#1973, #2509): inside the test file, every regression passed on the base too (#2536 review F1).
The regression tests stay in the test module. Standard library only; ``scan`` reads the files.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from env_read_scripts import (_PLACEHOLDERS, _fallback_matches, _fallback_reads, _joined,
                              _reads_a_hole_may_expose)

PREFIXES = ("DIAKTOROS_", "REVIEW_LOOP_")
HOME = Path("diaktoros/envnames.py")
SCRIPT, CODE = "generated script", "code"


def _is_environ(node) -> bool:
    """``os.environ`` (or a bare ``environ``)."""
    return ((isinstance(node, ast.Attribute) and node.attr == "environ"
             and isinstance(node.value, ast.Name) and node.value.id == "os")
            or (isinstance(node, ast.Name) and node.id == "environ"))


def _name_call(node):
    """``envnames.name("X")`` -> ``DIAKTOROS_X``, else ``None``."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "name" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "envnames"):
        arg = node.args[0] if node.args else None
        short = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else "?"
        return "DIAKTOROS_" + short
    return None


def bound_names(tree) -> dict[str, str]:
    """Names assigned an ``envnames.name(...)`` value or a prefixed literal: ``X_ENV = ...``."""
    bound = {}
    for node in ast.walk(tree):
        target = _bound_target(node)
        if target is None:
            continue
        value = node.value
        key = _name_call(value) or (
            value.value if isinstance(value, ast.Constant) and isinstance(value.value, str)
            and value.value.startswith(PREFIXES) else None)
        if key:
            bound[target] = key
    return bound


def _bound_target(node):
    """The name a binding assigns to: ``X = v``, ``X: str = v`` or ``(X := v)``, else ``None``."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and node.value is not None \
            and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.NamedExpr):
        return node.target.id
    return None


def _key(node, bound):
    """The variable a read names when this guard covers it, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value.startswith(PREFIXES) else None
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    if isinstance(node, ast.Attribute):          # gate_failures.REDRIVE_ENV
        return bound.get(node.attr)
    return _name_call(node)


def _call_key(node, bound):
    arg = node.args[0] if node.args else next(
        (kw.value for kw in node.keywords if kw.arg == "key"), None)
    return _key(arg, bound) if arg is not None else None


def _read_key(node, bound):
    """The covered variable ``node`` reads directly, else ``None``. Setting one
    (``os.environ[...] = ...``) is not a read; ``os.environ[...] += ...`` is."""
    if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
            and _is_environ(node.value)):
        return _key(node.slice, bound)
    if (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript)
            and _is_environ(node.target.value)):
        return _key(node.target.slice, bound)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.attr == "get" and _is_environ(func.value):
            return _call_key(node, bound)
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return _call_key(node, bound)
    return None


def _script_texts(node) -> list[str]:
    """The source a string constant holds, or an f-string's once per ``_PLACEHOLDERS`` entry."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(v.value if isinstance(v, ast.Constant) else hole for v in node.values)
                for hole in _PLACEHOLDERS]
    return []


def _fstring_parts(node) -> list:
    """An f-string's pieces in order: each literal fragment as a string, each interpolation as
    ``None``; ``[]`` for anything else."""
    if not isinstance(node, ast.JoinedStr):
        return []
    return [v.value if isinstance(v, ast.Constant) else None for v in node.values]


def _script_reads(texts: list[str], depth: int, parts=()) -> list[tuple[str, str]]:
    """The reads in a generated script given as candidate texts: the reading of the candidate
    that parses with the most reads, or, when none parses, the pattern search of the first.
    An f-string's clean parse does not prove a literal read absent (#2376): an interpolation
    inside a comment or a string literal can end it (``f"#{newline}os.getenv(...)"`` comments
    the read out of every placeholder version), so such reads are added
    (``_reads_a_hole_may_expose``)."""
    best, hole = None, None
    for index, text in enumerate(texts):
        try:
            ast.parse(textwrap.dedent(text))
        except SyntaxError:
            continue
        reads = direct_reads(text, None, depth)
        if best is None or len(reads) > len(best):
            best, hole = reads, _PLACEHOLDERS[index] if parts else None
    if best is None:
        text, holes = _joined(parts, _PLACEHOLDERS[0]) if parts else (texts[0], [])
        free = frozenset(h + len(_PLACEHOLDERS[0]) for h in holes)
        best = [(key, SCRIPT) for _, key in _fallback_matches(text, free=free)]
    keys = [k for k, _ in best]
    if hole is not None:
        keys += _reads_a_hole_may_expose(parts, hole, depth)
    return [(k, SCRIPT) for k in keys]


def direct_reads(source: str, bound=None, depth: int = 0) -> list[tuple[str, str]]:
    """``(variable, kind)`` for every covered direct read in ``source``, in walk order; kind is
    ``SCRIPT`` inside Python held in a string (a generated child-process script), else ``CODE``."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        if depth == 0:
            return []
        return [(key, SCRIPT) for key in _fallback_reads(source)]
    bound = {**(bound or {}), **bound_names(tree)}
    kind = SCRIPT if depth else CODE
    inner = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    found = []
    for node in ast.walk(tree):
        key = _read_key(node, bound)
        if key:
            found.append((key, kind))
            continue
        texts = [] if id(node) in inner else _script_texts(node)
        if depth < 2 and texts and ("environ" in texts[0] or "getenv" in texts[0]):
            found.extend(_script_reads(texts, depth + 1, _fstring_parts(node)))
    return found


def scan(root: Path, paths: list[Path]) -> dict[tuple[str, str, str], int]:
    """Every direct read in ``paths`` (under ``root``) outside ``envnames.py``: (file, variable,
    kind) -> count. A name bound to a covered variable in any scanned file counts in every file
    (``module.X_ENV``)."""
    paths = [p for p in paths if p.relative_to(root) != HOME]
    texts = {p: p.read_text(errors="replace") for p in paths}
    bound: dict[str, str] = {}
    for text in texts.values():
        try:
            bound.update(bound_names(ast.parse(text)))
        except SyntaxError:
            pass
    hits: dict[tuple[str, str, str], int] = {}
    for path, text in texts.items():
        for key, kind in direct_reads(text, bound):
            entry = (path.relative_to(root).as_posix(), key, kind)
            hits[entry] = hits.get(entry, 0) + 1
    return hits
