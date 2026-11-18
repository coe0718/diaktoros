"""The name resolution the paged-reads scan (``tools/paged_reads.py``) reads a path name with.

``_name_loads`` finds every load of a name that reads one scope's binding: in the scope's body and
in the nested functions and lambdas that capture it (#2363), past a nested scope that rebinds it
only where that scope evaluates its decorators and defaults (#2387) or a function in it declares
the name ``global`` (#2423, #2536 review F3), and not inside a comprehension that binds the name
itself (#2424). A name whose every load is a ``_read_pages`` path is paged.

It sits here, outside ``tests/``, with the scan, so the host's base-failure proof (``change_proof``,
which copies a PR's test paths onto the base) runs a PR's tests against the base's rules and not
the PR's (#1973, #2509): inside the test file, every regression passed on the base too (#2536
review F1). The regression tests stay in ``tests/test_paged_reads.py``. Standard library only.
"""
from __future__ import annotations

import ast

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _own(scope: ast.AST):
    """Every node of ``scope`` outside its nested functions and classes (they are scopes of
    their own: an outer ``_read_pages`` call says nothing about an inner read)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, DEFS):
            stack.extend(ast.iter_child_nodes(node))


def _binds(scope: ast.AST, name: str) -> bool:
    """Whether a nested function or class makes ``name`` its own (a parameter, an assignment
    or a nested ``def``/``class`` of that name, not declared ``nonlocal``/``global``)."""
    nodes = list(_own(scope))
    if any(isinstance(n, (ast.Nonlocal, ast.Global)) and name in n.names for n in nodes):
        return False
    args = getattr(scope, "args", None)
    if args is not None and name in {a.arg for a in [
            *args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg] if a}:
        return True
    return (any(n.id == name for n in _stores(scope))
            or any(isinstance(n, DEFS) and n.name == name for n in nodes))


def _stores(scope: ast.AST) -> list[ast.Name]:
    """The names ``scope``'s body binds by a store or ``del``. A comprehension's ``for`` target
    is the comprehension's own (#2387 review F3: ``[path for path in xs]`` does not rebind
    ``path``), but a ``:=`` in it binds in ``scope``. A nested ``def``, ``lambda`` or ``class``
    binds in its own scope, apart from the parts ``scope`` evaluates when defining it."""
    found: list[ast.Name] = []
    body = [scope.body] if isinstance(scope, ast.Lambda) else scope.body
    stack = [(child, False) for child in body]
    while stack:
        node, in_comp = stack.pop()
        if isinstance(node, ast.NamedExpr):
            found.append(node.target)
            stack.append((node.value, in_comp))
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load) and not in_comp:
            found.append(node)
        if isinstance(node, (*DEFS, ast.Lambda)):
            children = _evaluated_outside(node)
        else:
            children = ast.iter_child_nodes(node)
        stack += [(child, in_comp or isinstance(node, COMPS)) for child in children]
    return found


def _evaluated_outside(node: ast.AST) -> list[ast.AST]:
    """The parts of a ``def``, ``lambda`` or ``class`` that run in the enclosing scope when it
    is defined: decorators, parameter defaults and annotations, a class's bases and keywords
    (#2387 review F1: ``def g(path=gh.fetch(loop, path))`` reads the outer ``path``)."""
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    args = node.args
    parts = [*args.defaults, *(d for d in args.kw_defaults if d is not None)]
    if isinstance(node, ast.Lambda):
        return parts
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [*node.decorator_list, *parts, *(p.annotation for p in params if p and p.annotation),
            *([node.returns] if node.returns else [])]


def _name_loads(scope: ast.AST, name: str, module: bool = False) -> list[ast.Name]:
    """Every load of ``name`` that reads ``scope``'s binding: in ``scope``'s body and in the
    nested functions and lambdas that capture it (#2363: a closure's ``fetch(loop, path)``
    reads ``path`` too). A nested function or lambda that rebinds ``name`` reads its own in
    its body, but its decorators and defaults still read ``scope``'s (#2387 F1). A nested
    class body is walked as part of ``scope`` even when it assigns ``name``: at module level
    a load before that assignment reads the module's name, and in a function it is a
    ``NameError``, so counting it only ever reports a site, never clears one. The functions
    in a class are nested scopes; a class's own binding does not reach its methods.

    A function that rebinds ``name`` may still hold one that declares it ``global``, which
    reads the module's binding (#2423, ``_global_loads``): so wherever ``scope``'s ``name``
    is the module's (``module``: the module itself, a function declaring it ``global``, or one
    capturing it from either), a rebinding function is searched for a deeper ``global``
    (#2536 review F3: under a ``global`` function, a shadowing one hid the ``global`` below
    it). A comprehension whose ``for`` target is ``name`` reads its own ``name`` everywhere
    but its first ``iter`` (#2424, ``_comp_binds``)."""
    module = module or isinstance(scope, ast.Module)
    loads: list[ast.Name] = []
    stack = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            loads.append(node)
        elif isinstance(node, (*DEFS, ast.Lambda)):
            stack += _evaluated_outside(node)
            if isinstance(node, ast.ClassDef):
                stack += node.body
            elif not isinstance(scope, ast.ClassDef) and not _binds(node, name):
                loads += _name_loads(node, name, module)
            elif module:
                loads += _global_loads(node, name)
        elif isinstance(node, COMPS) and _comp_binds(node, name):
            stack.append(node.generators[0].iter)
        else:
            stack += ast.iter_child_nodes(node)
    return loads


def _comp_binds(comp: ast.AST, name: str) -> bool:
    """Whether a comprehension's ``for`` targets bind ``name``, making it the comprehension's
    own (#2424)."""
    return any(isinstance(n, ast.Name) and n.id == name
               for gen in comp.generators for n in ast.walk(gen.target))


def _global_loads(shadow: ast.AST, name: str) -> list[ast.Name]:
    """The loads of the module's ``name`` inside ``shadow``, a nested scope that rebinds it: a
    function anywhere in it that declares ``global name`` reads the module's binding, and so
    do the functions nested in that one (#2423: ``def outer(): path = "/own"; def inner():
    global path; return gh.fetch(None, path)`` read the module's path unpaged). ``nonlocal``
    cannot reach past ``shadow``: the nearest enclosing function binding it is ``shadow`` or
    one inside it. The search stops at such a function: ``_name_loads`` reads it whole, a
    deeper shadow included (#2536 review F3), so no load is counted twice."""
    loads: list[ast.Name] = []
    stack = list(ast.iter_child_nodes(shadow))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(n, ast.Global) and name in n.names for n in _own(node)):
            loads += _name_loads(node, name, module=True)
        else:
            stack += ast.iter_child_nodes(node)
    return loads
