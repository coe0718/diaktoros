"""The paged-reads scan that ``tests/test_paged_reads.py`` enforces (#2299).

A full first page read as the whole list was a real bug three times (#558 hooks, #559 reviews,
#562 annotations). ``sites`` finds every string (f-strings included, docstrings not) holding
``per_page=100`` and says, for each string on its own (#2299 review F1), whether that string
itself reaches ``gh._read_pages``: as part of the path argument of a ``_read_pages`` call in its
own function (the argument, an f-string field or a ``+`` operand, never another call's argument),
through a name whose every use is such a path (``tools/paged_read_names.py``), or returned by a
path builder whose every call is one or is only text for ``log`` or a failure record. The
registry of the reads that may stay unpaged, with their reasons, is the test's.

It sits here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's scan and not the PR's
(#1973, #2509): inside the test file, every regression passed on the base too (#2536 review F1).
The regression tests stay in the test module. Standard library only; no I/O beyond ``scan``
reading the scanned trees.
"""
from __future__ import annotations

import ast
from pathlib import Path

import paged_read_names
from paged_read_names import DEFS, _own

ROOT = Path(__file__).resolve().parents[1]
MARK = "per_page=100"
SCANNED = ("diaktoros", "scripts")
READER = "_read_pages"

# Where a path builder's result may go besides ``_read_pages``: a log line or the record of a
# failed read, directly or through the text built for one. Neither reads the listing. Text
# built for anything else (``gh.fetch(loop, f"{hooks_path(loop)}&a=1")``) is a read (#2299
# review F1, round 2).
NOT_A_READ = {"record_failure", "log"}


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)


def _scopes(tree: ast.Module):
    """``(qualified name, node)`` for the module and every function and class in it."""
    out = [("<module>", tree)]

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, DEFS):
                out.append((prefix + child.name, child))
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)
    visit(tree, "")
    return out


def _path_arg(call: ast.Call) -> ast.AST | None:
    """The ``path`` argument of a ``_read_pages(loop, path, ...)`` call."""
    if len(call.args) > 1:
        return call.args[1]
    return next((k.value for k in call.keywords if k.arg == "path"), None)


def _path_parts(node: ast.AST):
    """``node`` and every node whose text becomes part of it as a path: the fields of an
    f-string and the operands of ``+`` or ``%``. A call's arguments are not part of its result,
    so ``_read_pages(loop, fetch(loop, "/x?per_page=100"), ...)`` does not page that string."""
    yield node
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            yield from _path_parts(value)
    elif isinstance(node, ast.FormattedValue):
        yield from _path_parts(node.value)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        yield from _path_parts(node.left)
        yield from _path_parts(node.right)


def _part_ids(node: ast.AST) -> set[int]:
    return {id(part) for part in _path_parts(node)}


def _reader_paths(nodes) -> list[ast.AST]:
    return [arg for node in nodes if isinstance(node, ast.Call) and _called_name(node) == READER
            for arg in [_path_arg(node)] if arg is not None]


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return node.value


def _strings(scope: ast.AST) -> list[ast.AST]:
    """Each string in ``scope`` holding ``MARK`` (an f-string as one), docstrings excluded."""
    nodes = list(_own(scope))
    skip = {id(n.value) for n in nodes
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    skip |= {id(v) for n in nodes if isinstance(n, ast.JoinedStr) for v in n.values}
    found = [n for n in nodes if isinstance(n, ast.JoinedStr) and any(
        isinstance(v, ast.Constant) and MARK in str(v.value) for v in n.values)]
    found += [n for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and MARK in n.value and id(n) not in skip]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def _into_reader_by_name(site: ast.AST, scope: ast.AST) -> bool:
    """``path = "...per_page=100..."`` whose every later use, nested functions included, is
    the path ``_read_pages`` reads (``path += ...`` may extend it): a name also handed to
    another read is not paged."""
    for node in _own(scope):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and id(site) in _part_ids(node.value)):
            loads = paged_read_names._name_loads(scope, node.targets[0].id)
            paged = set().union(*(_part_ids(a) for a in _reader_paths(ast.walk(scope))))
            return bool(loads) and all(id(n) in paged for n in loads)
    return False


def _use(call: ast.Call, parents: dict[int, ast.AST], reader_parts: set[int]) -> str:
    """Where a call's result ends up: ``"read"`` when it is part of the path ``_read_pages``
    reads, ``"shown"`` when the text it is part of is only an argument of a ``NOT_A_READ`` call,
    ``"other"`` for anything else (an f-string handed to ``fetch`` is a read of one page)."""
    if id(call) in reader_parts:
        return "read"
    node = call                    # climb to the whole text this call's result is part of
    while id(node) in parents and id(node) in _part_ids(parents[id(node)]):
        node = parents[id(node)]
    consumer = parents.get(id(node))
    if (isinstance(consumer, ast.Call) and _called_name(consumer) in NOT_A_READ
            and node in consumer.args):
        return "shown"
    return "other"


def _feeders(trees: dict[str, ast.Module]) -> set[str]:
    """Path builders whose every call, anywhere scanned, is the path ``_read_pages`` reads (or
    is only logged or recorded): ``hooks_path`` and ``reviews_path``."""
    uses: dict[str, set[str]] = {}         # name -> {"read", "shown", "other"}
    for tree in trees.values():
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        reader_parts = set().union(*(_part_ids(a) for a in _reader_paths(ast.walk(tree))))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and _called_name(call) is not None:
                uses.setdefault(_called_name(call), set()).add(
                    _use(call, parents, reader_parts))
    return {name for name, kinds in uses.items() if "read" in kinds and "other" not in kinds}


def sites(sources: dict[str, str]) -> dict[str, bool]:
    """``{"rel::function::text": paged}`` for every ``per_page=100`` string in ``sources``
    (``{rel: source}``). A string is paged only when that string itself reaches ``_read_pages``:
    inside its path argument, through a name used for nothing else, or returned by a path
    builder (``_feeders``) whose every call is such an argument."""
    trees = {rel: ast.parse(source) for rel, source in sources.items()}
    feeders = _feeders(trees)
    found: dict[str, bool] = {}
    for rel, tree in trees.items():
        for qualname, scope in _scopes(tree):
            nodes = list(_own(scope))
            reader_args = set().union(*(_part_ids(a) for a in _reader_paths(nodes)))
            returned = set().union(*(_part_ids(n.value) for n in nodes
                                     if isinstance(n, ast.Return) and n.value is not None))
            for site in _strings(scope):
                key = base = f"{rel}::{qualname}::{_text(site)}"
                count = 1
                while key in found:
                    count += 1
                    key = f"{base}#{count}"
                found[key] = (id(site) in reader_args
                              or _into_reader_by_name(site, scope)
                              or (id(site) in returned and isinstance(scope, ast.FunctionDef)
                                  and scope.name in feeders))
    return found


def scan() -> dict[str, bool]:
    sources = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" not in path.parts:
                sources[path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return sites(sources)
