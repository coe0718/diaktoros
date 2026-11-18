"""Where a run_supervisor submodule name is read before its local import binds it (#2509, #2522).

The read side of the submodule-shadow rules in ``tools/submodule_shadows.py``: ``scope_nodes``
walks one module or function scope, and ``unbound_read`` finds the first read of a name in a
function that may run before that function's own import of the name has bound it, which raises
UnboundLocalError even when the module imports the same submodule.

It sits here, outside ``tests/``, with the rules, so the host's base-failure proof
(``change_proof``, which copies a PR's test paths onto the base) runs a PR's tests against the
base's rules and not the PR's (#1973, #2509). The regression tests stay in
``tests/test_run_supervisor_modules.py``. Standard library only.
"""
from __future__ import annotations

import ast


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



_TRIES = tuple(getattr(ast, n) for n in ("Try", "TryStar") if hasattr(ast, n))


def _imports(node: ast.AST, name: str) -> bool:
    """Whether ``node`` is an import statement that binds ``name``."""
    return (isinstance(node, (ast.Import, ast.ImportFrom))
            and any((a.asname or a.name.split(".")[0]) == name for a in node.names))


def _deletes(stmts: list, name: str) -> bool:
    """Whether a ``del`` anywhere in these statements may unbind ``name`` (#2536 review F6)."""
    return any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Del) and n.id == name
               for stmt in stmts for n in ast.walk(stmt))


def _reads(nodes, name: str) -> list[tuple[int, int]]:
    """(line, column) of each read of ``name`` in these nodes, nested scopes' bodies excluded."""
    return [(n.lineno, n.col_offset) for n in _scope_walk(list(nodes), module=False)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == name]


def _block_reads(stmts: list, name: str) -> list[tuple[int, int]]:
    """The reads of ``name`` in this statement block that run before an import of it in the
    same block, or in a block that holds this one, has bound it. Each nested block (the bodies of
    an if, loop, with, try, except handler or match case) starts unbound: an import inside one
    binds the rest of that block only, since the block may not run (``if False: import ...``).
    The one exception is a ``try``'s ``else``: it runs only once the ``try`` body has finished, so
    an import at that body's top level has bound it (#2522), unless that body also ``del``s the
    name (#2536 review F6)."""
    found = []
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found += _reads([stmt], name)          # decorators, bases, defaults: not the body
        else:
            bound_else = (isinstance(stmt, _TRIES) and any(_imports(s, name) for s in stmt.body)
                          and not _deletes(stmt.body, name))
            for field, value in ast.iter_fields(stmt):
                if bound_else and field == "orelse":
                    continue
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
    ``views.x()`` after the ``if`` is not, and neither is a read after a ``try`` whose body
    imports it (an exception may skip the import); its ``else`` is bound (#2522) unless that body
    ``del``s it. Not seen: a read in a nested
    function or lambda (a closure cell), a ``del`` of the name after its import, and a read in a
    loop body that follows the import in the text but runs first on a later iteration."""
    if not any(_imports(n, name) for n in scope_nodes(func)):
        return None
    reads = _block_reads(func.body, name)
    return min(reads)[0] if reads else None
