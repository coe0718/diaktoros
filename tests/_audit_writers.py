"""The drift guard's model of the audit writers (``test_audit_vocabulary``): every function and
call in ``diaktoros/`` (or in synthetic sources a test hands it), and a resolver for the strings
a role or operation argument can hold.

A variable is followed to where it comes from: an assignment, the callers that pass it (across
modules), or a guard that refuses anything outside a fixed list. A guard counts only where it
*dominates* the use (#2473 review): ``if name not in (...): raise`` as a top-level statement of
the function (or in a ``try`` whose handlers all re-raise), before the statement that uses the
value, in a function that never rebinds the name. A guard handed on (``callee(name=name)``)
counts only as such a statement of its own, and only when every function of that name bounds it
before any ``return``. Anything else (a guard under a condition, a loop or a swallowing ``try``,
after the write, or past an early return) bounds nothing, so the value is followed to its callers
instead, and fails if it cannot be.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef)


class Unresolved(AssertionError):
    pass


def _own(nodes):
    """The nodes under ``nodes``, not those of a function nested in them."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, _SCOPES))


class Writers:
    def __init__(self, sources=None):
        """``sources``: ``(filename, text)`` pairs; every ``diaktoros/*.py`` when omitted."""
        if sources is None:
            sources = [(str(path), path.read_text())
                       for path in sorted((ROOT / 'diaktoros').rglob('*.py'))]
        self.defs, self.calls, self.consts = {}, {}, {}
        for filename, text in sources:
            tree = ast.parse(text, filename)
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.consts[target.id] = node.value
            self._index(tree, None)

    def _index(self, node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(child.name, []).append(child)
                self._index(child, child)
                continue
            if isinstance(child, ast.Call):
                self.calls.setdefault(self.name(child.func), []).append((child, fn))
            self._index(child, fn)

    @staticmethod
    def name(func):
        return getattr(func, 'attr', getattr(func, 'id', ''))

    @staticmethod
    def body(fn):
        """``fn``'s own nodes, not those of a function nested in it."""
        return _own(fn.body)

    def strings(self, node, fn, depth=0):
        where = f'{fn.name if fn else "<module>"}:{getattr(node, "lineno", "?")}'
        if depth > 8:
            raise Unresolved(where)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return self.strings(node.body, fn, depth + 1) | self.strings(node.orelse, fn, depth + 1)
        if isinstance(node, ast.Tuple):
            return set().union(*(self.strings(item, fn, depth + 1) for item in node.elts))
        if isinstance(node, ast.JoinedStr):
            parts = [self.strings(part.value if isinstance(part, ast.FormattedValue) else part,
                                  fn, depth + 1) for part in node.values]
            return {''.join(combo) for combo in itertools.product(*parts)}
        if (isinstance(node, ast.Call) and self.name(node.func) == 'removeprefix'
                and isinstance(node.func, ast.Attribute) and len(node.args) == 1):
            [prefix] = self.strings(node.args[0], fn, depth + 1)
            return {value.removeprefix(prefix)
                    for value in self.strings(node.func.value, fn, depth + 1)}
        if isinstance(node, ast.Name) and fn is not None:
            return self.variable(node.id, fn, depth, where, node.lineno)
        raise Unresolved(where)

    @staticmethod
    def _rebinds(name, fn):
        return any((isinstance(node, ast.Name) and node.id == name
                    and not isinstance(node.ctx, ast.Load))
                   or (isinstance(node, ast.ExceptHandler) and node.name == name)
                   for node in _own(fn.body))

    @classmethod
    def _flat(cls, stmts):
        """``stmts``, with the body of a ``try`` that cannot swallow an exception (every handler
        ends by raising, and no handler or ``finally`` can return) in place of the ``try``: its
        statements all run before the next one, or nothing after it does."""
        for stmt in stmts:
            if isinstance(stmt, ast.Try) and all(
                    handler.body and isinstance(handler.body[-1], ast.Raise)
                    for handler in stmt.handlers) and not any(
                    isinstance(node, (ast.Return, ast.Break, ast.Continue))
                    for node in _own(stmt.handlers + stmt.finalbody)):
                yield from cls._flat(stmt.body + stmt.orelse)
            else:
                yield stmt

    @classmethod
    def _dominating(cls, fn, use):
        """``fn``'s top-level statements that run before the one holding line ``use`` (or, with
        no ``use``, before any statement that can ``return``)."""
        for index, stmt in enumerate(fn.body):
            if use is not None and stmt.lineno <= use <= stmt.end_lineno:
                return list(cls._flat(fn.body[:index]))
            if use is None and any(isinstance(node, ast.Return) for node in _own([stmt])):
                return list(cls._flat(fn.body[:index]))
        return [] if use is not None else list(cls._flat(fn.body))

    def guard(self, name, fn, depth=0, use=None):
        """The values ``fn`` refuses everything outside of before line ``use`` (before any
        ``return`` when ``use`` is omitted), with ``if name not in (...): raise`` or by handing
        ``name=name`` to a function that does; ``None`` when nothing provably bounds it."""
        if self._rebinds(name, fn):
            return None
        bounds = []
        for stmt in self._dominating(fn, use):
            if isinstance(stmt, ast.If) and len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Raise):
                tests = stmt.test.values if (isinstance(stmt.test, ast.BoolOp)
                                             and isinstance(stmt.test.op, ast.Or)) else [stmt.test]
                for test in tests:
                    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                            and test.left.id == name and [type(op) for op in test.ops] == [ast.NotIn]):
                        allowed = test.comparators[0]
                        allowed = self.consts.get(getattr(allowed, 'id', None), allowed)
                        bounds.append(self.strings(allowed, None, depth + 1))
            call = getattr(stmt, 'value', None) if isinstance(
                stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
            if isinstance(call, ast.Call) and depth < 3 and any(
                    kw.arg == name and isinstance(kw.value, ast.Name) and kw.value.id == name
                    for kw in call.keywords):
                found = [self.guard(name, callee, depth + 1)
                         for callee in self.defs.get(self.name(call.func), [])]
                if found and None not in found:
                    bounds.append(set().union(*found))
        return set.intersection(*bounds) if bounds else None

    def variable(self, name, fn, depth, where, use=None):
        bound = self.guard(name, fn, use=use)
        if bound is not None:
            return bound
        values = set()
        assigned = [node.value for node in self.body(fn) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)]
        for value in assigned:
            values |= self.strings(value, fn, depth + 1)
        args = fn.args
        positional = [a.arg for a in args.posonlyargs + args.args]
        if name not in positional + [a.arg for a in args.kwonlyargs]:
            if assigned:
                return values
            raise Unresolved(where)
        index = positional.index(name) if name in positional else None
        default = self._default(fn, name)
        for call, caller in self.calls.get(fn.name, []):
            passed = next((kw.value for kw in call.keywords if kw.arg == name), None)
            if passed is None and index is not None and index < len(call.args):
                passed = call.args[index]
            if passed is not None:
                values |= self.strings(passed, caller, depth + 1)
            elif default is not None:
                values |= self.strings(default, fn, depth + 1)
        return values

    @staticmethod
    def _default(fn, name):
        args = fn.args
        positional = args.posonlyargs + args.args
        pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        return next((d for a, d in pairs if a.arg == name), None)

    def written(self):
        """``(roles, operations)`` every audit writer can record: each ``_audit(…, role,
        operation, login)`` call and each record dict naming a role, an operation and a login."""
        roles, operations = set(), set()
        for call, fn in self.calls.get('_audit', []):
            if len(call.args) >= 7:
                roles |= self.strings(call.args[5], fn)
                operations |= self.strings(call.args[6], fn)
        for defs in self.defs.values():
            for fn in defs:
                for node in self.body(fn):
                    if not isinstance(node, ast.Dict):
                        continue
                    keys = {k.value: v for k, v in zip(node.keys, node.values)
                            if isinstance(k, ast.Constant)}
                    if {'role', 'operation', 'login'} <= set(keys):
                        roles |= self.strings(keys['role'], fn)
                        operations |= self.strings(keys['operation'], fn)
        return roles, operations
