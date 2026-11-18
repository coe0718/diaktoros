"""The submodule-shadow rules that ``tests/test_run_supervisor_modules.py`` enforces (#1458, #1665).

A function that binds a local named like a ``run_supervisor`` submodule it calls turns a later
``views.<name>`` into an UnboundLocalError (``facts.pr_record`` once resolved to a local dict).
Two rules check it, both driven by ``symtable`` so every binding shape counts:

* ``local_submodule_shadows``: the package's own functions, against every submodule name;
* ``shadowed_submodules``: any source (the tests/ files), against the names that source imports
  as a submodule, under any alias, at module level and in every function.

They sit here, outside ``tests/``, so the host's base-failure proof (``change_proof``, which copies
a PR's test paths onto the base) runs a PR's tests against the base's rules and not the PR's
(#1973, #2509); their regression tests stay in the test module. The read side, ``scope_nodes``
and ``unbound_read``, is ``tools/submodule_unbound_reads.py``. Standard library only; no I/O
beyond listing the package's submodules.
"""
from __future__ import annotations

import ast
import symtable
from pathlib import Path

# The read side (scope walking, reads before a local import) is tools/submodule_unbound_reads.py.
from submodule_unbound_reads import scope_nodes, unbound_read

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
    may run before (see ``unbound_read``, in ``tools/submodule_unbound_reads.py``), which raises
    UnboundLocalError."""
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
