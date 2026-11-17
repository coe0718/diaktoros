"""The submodule-shadow rules that ``tests/test_run_supervisor_modules.py`` enforces (#1458, #1665).

A function that binds a local named like a ``run_supervisor`` submodule it calls turns a later
``views.<name>`` into an UnboundLocalError (``facts.pr_record`` once resolved to a local dict).
Two rules check it, both driven by ``symtable`` so every binding shape counts:

* ``local_submodule_shadows``: the package's own functions, against every submodule name;
* ``shadowed_submodules``: any source (the tests/ files), against the names that source imports
  as a submodule, under any alias, at module level and in every function.

They sit here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's rules and not the PR's
(#1973, #2509); their regression tests stay in the test module. Standard library only; no I/O
beyond listing the package's submodules.
"""
from __future__ import annotations

import ast
import symtable
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "diaktoros" / "run_supervisor"
# Every real submodule of the package, ``turns`` included (#1528).
ALL_SUBMODULES = tuple(sorted(
    p.stem for p in PACKAGE.glob("*.py") if p.stem not in ("__init__", "__main__")))

# Symtable names of the comprehension scopes. Before Python 3.12 (PEP 709) every comprehension
# is a child "function" scope of this name; from 3.12 only a generator expression still is. A
# real ``def listcomp`` has the same name, so a scope is a comprehension only when no function
# of that name starts on its line (#2509).
_COMPREHENSION_SCOPES = ("listcomp", "setcomp", "dictcomp", "genexpr")


def local_submodule_shadows(source: str, filename: str) -> list[tuple[str, str]]:
    """(function, name) for each function in the source with a local binding named like any
    run_supervisor submodule: one not imported, or a local import of it that a read of the name
    may run before (see ``unbound_read``), which raises UnboundLocalError."""
    found = []
    functions = function_nodes(ast.parse(source))

    def walk(table):
        for child in table.get_children():
            if child.get_type() == "function":
                node = functions.get((child.get_name(), child.get_lineno()))
                for sub in ALL_SUBMODULES:
                    symbol = child.lookup(sub) if sub in child.get_identifiers() else None
                    if symbol is None or not symbol.is_local():
                        continue
                    if not symbol.is_imported() or (
                            node is not None and unbound_read(node, sub) is not None):
                        found.append((child.get_name(), sub))
            walk(child)
    walk(symtable.symtable(source, filename, "exec"))
    return found


def submodule_import(node: ast.AST, alias: ast.alias) -> bool:
    """Whether this import alias binds a run_supervisor submodule: ``from
    diaktoros.run_supervisor import views [as v]`` or ``import diaktoros.run_supervisor.views as
    v`` (#1665)."""
    if isinstance(node, ast.ImportFrom):
        return node.module == "diaktoros.run_supervisor" and alias.name in ALL_SUBMODULES
    prefix = "diaktoros.run_supervisor."
    return (alias.asname is not None and alias.name.startswith(prefix)
            and alias.name[len(prefix):] in ALL_SUBMODULES)


def submodule_aliases(tree: ast.AST) -> set[str]:
    """Every name the source binds to a run_supervisor submodule by an import, at any scope and
    under any alias."""
    return {a.asname or a.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for a in node.names if submodule_import(node, a)}


def scope_nodes(scope: ast.AST):
    """Each AST node that belongs directly to one module or function scope. Nested function,
    lambda and class bodies are their own scopes and are not entered; their decorators, bases
    and defaults are. At module level a comprehension's own targets are skipped as well: Python
    gives a comprehension its own scope, so ``[views for views in ()]`` there rebinds nothing in
    the module (#2509). Inside a function they are kept, as the package guard and the tests/
    guard before #1665 count them."""
    body = list(scope.body) if isinstance(scope.body, list) else [scope.body]
    return _scope_walk(body, module=isinstance(scope, ast.Module))


def _scope_walk(stack: list, module: bool):
    """Each node of these roots and below them that belongs to their scope (see scope_nodes);
    ``module`` says whether comprehension targets are their own scope's."""
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend([*node.bases, *node.keywords])
            else:
                stack.extend([*node.args.defaults, *(d for d in node.args.kw_defaults if d)])
        elif isinstance(node, ast.Lambda):
            stack.extend([*node.args.defaults, *(d for d in node.args.kw_defaults if d)])
        elif module and isinstance(node, ast.comprehension):
            stack.extend([node.iter, *node.ifs])
        else:
            stack.extend(ast.iter_child_nodes(node))


def scope_bindings(scope: ast.AST) -> list[tuple[int, str, bool]]:
    """(line, name, is_submodule_import) for each name bound directly in a module or function
    scope (see scope_nodes for what belongs to it): assignment, for/with/walrus/comprehension
    targets, arguments, ``except ... as``, imports, def/class names and match captures."""
    found = []
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                found.append((arg.lineno, arg.arg, False))
    for node in scope_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.lineno, node.name, False))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.append((node.lineno, node.id, False))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.lineno, node.name, False))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            found.append((node.lineno, node.name, False))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            found.append((node.lineno, node.rest, False))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                found.append((node.lineno, a.asname or a.name.split(".")[0],
                              submodule_import(node, a)))
    return found


def _imports(node: ast.AST, name: str) -> bool:
    """Whether ``node`` is an import statement that binds ``name``."""
    return (isinstance(node, (ast.Import, ast.ImportFrom))
            and any((a.asname or a.name.split(".")[0]) == name for a in node.names))


def _reads(nodes, name: str) -> list[tuple[int, int]]:
    """(line, column) of each read of ``name`` in these nodes, nested scopes' bodies excluded."""
    return [(n.lineno, n.col_offset) for n in _scope_walk(list(nodes), module=False)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name]


def _block_reads(stmts: list, name: str) -> list[tuple[int, int]]:
    """The reads of ``name`` in this statement block that run before an import of it in the
    same block, or in a block that holds this one, has bound it. Each nested block (the bodies of
    an if, loop, with, try, except handler or match case) starts unbound: an import inside one
    binds the rest of that block only, since the block may not run (``if False: import ...``)."""
    found = []
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found += _reads([stmt], name)          # decorators, bases, defaults: not the body
        else:
            for _field, value in ast.iter_fields(stmt):
                items = value if isinstance(value, list) else [value]
                if items and isinstance(items[0], ast.stmt):
                    found += _block_reads(items, name)
                elif items and isinstance(items[0], (ast.ExceptHandler, ast.match_case)):
                    for part in items:      # an except handler or a match case
                        head = [v for f, v in ast.iter_fields(part) if f != "body"]
                        found += _reads([v for v in head if isinstance(v, ast.AST)], name)
                        found += _block_reads(part.body, name)
                else:
                    found += _reads([i for i in items if isinstance(i, ast.AST)], name)
        if _imports(stmt, name):
            break                                   # bound for the rest of this block
    return found


def unbound_read(func: ast.AST, name: str) -> int | None:
    """The line of the first read of ``name`` in this function that may run before the
    function's own import of it has bound it, or None when the function has no such import or
    no such read (#2509). A local import makes the name local to the whole function, so such a
    read raises UnboundLocalError even when the module imports the very same submodule.

    An import binds the statements after it in its own block and in the blocks those hold (see
    _block_reads), so ``try: import views; views.x()`` is safe while ``if c: import views`` then
    ``views.x()`` after the ``if`` is not. Conservative where a later block always runs after
    the import (a ``try``'s ``else``): that read is reported too. Not seen: a read in a nested
    function or lambda (a closure cell), a ``del`` of the name after its import, and a read in a
    loop body that follows the import in the text but runs first on a later iteration."""
    if not any(_imports(n, name) for n in scope_nodes(func)):
        return None
    reads = _block_reads(func.body, name)
    return min(reads)[0] if reads else None


def function_nodes(tree: ast.AST) -> dict[tuple[str, int], ast.AST]:
    """Each function and lambda of the tree, by the (name, line) symtable gives its scope."""
    return {("lambda" if isinstance(n, ast.Lambda) else n.name, n.lineno): n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))}


def shadowed_submodules(source: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """(line, name) for each binding, at module level or in a function, of a name the source
    imports as a run_supervisor submodule, other than such an import itself (#1458, #1665), and
    for each read in a function that may run before that function's own import of the name
    (``unbound_read``, #2509).

    Function locals come from symtable, as in local_submodule_shadows, so every binding shape
    counts; scope_bindings only supplies the line and tells a submodule import from any other
    binding of the name. A local symtable reports that has no binding line found is reported at
    the function's own line. A comprehension at module level is its own scope and rebinds
    nothing in the module (#2509); one inside a function counts as part of that function."""
    tree = ast.parse(source)
    aliases = submodule_aliases(tree)
    if not aliases:
        return []
    functions = function_nodes(tree)
    found = {(line, name) for line, name, submodule in scope_bindings(tree)
             if name in aliases and not submodule}

    def walk(table, in_function):
        for child in table.get_children():
            if child.get_type() != "function":
                walk(child, in_function)
                continue
            key = (child.get_name(), child.get_lineno())
            if key[0] in _COMPREHENSION_SCOPES and key not in functions and not in_function:
                walk(child, False)      # before Python 3.12: a module-level comprehension
                continue
            found.update(_function_shadows(child, functions.get(key), aliases))
            walk(child, True)
    walk(symtable.symtable(source, filename, "exec"), False)
    return sorted(found)


def _function_shadows(table, node, aliases) -> set[tuple[int, str]]:
    """(line, name) for each name of ``aliases`` that one function scope (its symtable and, when
    found, its AST node) rebinds, or reads before its own import of it has bound it (#2509)."""
    bindings = scope_bindings(node) if node is not None else []
    found = set()
    for name in aliases & set(table.get_identifiers()):
        symbol = table.lookup(name)
        # A `global`/`nonlocal` name assigned here rebinds the import as well.
        if not (symbol.is_local() or symbol.is_declared_global() or symbol.is_nonlocal()):
            continue
        hits = {(line, n) for line, n, submodule in bindings if n == name and not submodule}
        if not hits and symbol.is_local() and not symbol.is_imported():
            hits = {(table.get_lineno(), name)}
        # Only a local import leaves a read unbound: under `global`/`nonlocal` the import
        # rebinds the outer name, which an earlier read still finds (#2509 F4).
        early = unbound_read(node, name) if node is not None and symbol.is_local() else None
        if early is not None:
            hits.add((early, name))
        found.update(hits)
    return found
