"""The broker audit's roles and operations, pinned to every writer and to both readers.

#2293 named every audit role; #2404: the operations a writer records read back as themselves on
the Desktop, not ``unknown``. #2473: the drift guard sees every writer, not only literal roles.
A role or operation that reaches an audit line through a variable is followed to where it comes
from: an assignment, the callers that pass it (across modules), or a guard that refuses anything
outside a fixed list before the write (``broker.authorize``'s seats, ``PERFORMED_OPERATIONS``).
A value it cannot follow fails the test, so a new writer can never pass it unseen.
"""
import _home_guard  # noqa: F401
import ast
import itertools
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from diaktoros import broker, config, desktop_api, gh
from diaktoros.broker import filing

ROOT = Path(__file__).resolve().parents[1]


class Unresolved(AssertionError):
    pass


class Writers:
    """Every function and call in ``diaktoros/``, and a resolver for the strings an argument
    can hold."""

    def __init__(self):
        self.defs, self.calls, self.consts = {}, {}, {}
        for path in sorted((ROOT / 'diaktoros').rglob('*.py')):
            tree = ast.parse(path.read_text(), str(path))
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
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            yield node
            stack.extend(child for child in ast.iter_child_nodes(node)
                         if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))

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
            return self.variable(node.id, fn, depth, where)
        raise Unresolved(where)

    def guard(self, name, fn, depth=0):
        """The values ``fn`` (or a function it hands ``name=name`` to) refuses everything outside
        of, with ``if name not in (...): raise``; ``None`` when nothing bounds it."""
        bounds = []
        for node in self.body(fn):
            if (isinstance(node, ast.If) and len(node.body) == 1
                    and isinstance(node.body[0], ast.Raise)):
                tests = node.test.values if (isinstance(node.test, ast.BoolOp)
                                             and isinstance(node.test.op, ast.Or)) else [node.test]
                for test in tests:
                    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                            and test.left.id == name and [type(op) for op in test.ops] == [ast.NotIn]):
                        allowed = test.comparators[0]
                        allowed = self.consts.get(getattr(allowed, 'id', None), allowed)
                        bounds.append(self.strings(allowed, None, depth + 1))
            if isinstance(node, ast.Call) and depth < 3 and any(
                    kw.arg == name and isinstance(kw.value, ast.Name) and kw.value.id == name
                    for kw in node.keywords):
                for callee in self.defs.get(self.name(node.func), []):
                    found = self.guard(name, callee, depth + 1)
                    if found is not None:
                        bounds.append(found)
        return set.intersection(*bounds) if bounds else None

    def variable(self, name, fn, depth, where):
        bound = self.guard(name, fn)
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


def page_list(field):
    page = (ROOT / 'desktop/plugin.js').read_text()
    found = re.search(r"category\(row\." + field + r", \[([^\]]*)\]\)", page)
    assert found is not None
    return re.findall(r"'([a-z_]+)'", found.group(1))


class EveryWriterIsSeen(unittest.TestCase):
    def test_roles_and_operations_cover_every_writer_and_the_page_validator(self):
        roles, operations = Writers().written()
        self.assertEqual(roles, set(broker.AUDIT_ROLES))
        self.assertEqual(operations, set(broker.AUDIT_OPERATIONS))
        self.assertEqual(page_list('role'), [*broker.AUDIT_ROLES, 'unknown'])
        self.assertEqual(page_list('operation'), [*broker.AUDIT_OPERATIONS, 'unknown'])

    def test_a_role_passed_as_a_variable_is_followed_to_its_bound(self):
        # #2473: broker.perform's role is a variable; authorize admits only these seats.
        writers = Writers()
        [perform] = [fn for fn in writers.defs['perform']
                     if any(a.arg == 'branch' for a in fn.args.kwonlyargs)]
        self.assertEqual(writers.guard('role', perform), {'reviewer', 'fixer'})
        self.assertEqual(writers.guard('operation', perform), set(broker.PERFORMED_OPERATIONS))

    def test_an_unfollowable_role_fails_the_guard(self):
        tree = ast.parse("def write(loop, scope):\n"
                         "    _audit(loop, 'r', 1, '', '', scope.role, 'review', 'x')\n")
        writers = Writers()
        fn = tree.body[0]
        with self.assertRaises(Unresolved):
            writers.strings(fn.body[0].value.args[5], fn)


class OperationsReadBack(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.loop = {'id': 'widgets', 'repo': 'acme/widgets',
                     'state_dir': str(Path(temp.name) / 'state')}
        config.state_dir(self.loop).mkdir()
        for patcher in (patch.object(config, 'readable_loops', return_value=([self.loop], [])),
                        patch.object(gh, 'api', side_effect=AssertionError('network forbidden'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def latest(self):
        return desktop_api.records_view('widgets')['broker']['items'][0]

    def test_every_written_operation_is_named_not_unknown(self):
        for operation in broker.AUDIT_OPERATIONS:
            with self.subTest(operation=operation):
                broker._audit(self.loop, self.loop['repo'], 12, 'a' * 40, '', 'reader', operation, 'x')
                self.assertEqual(self.latest()['operation'], operation)
        broker._audit(self.loop, self.loop['repo'], 12, 'a' * 40, '', 'reader', '/home/x', 'x')
        self.assertEqual(self.latest()['operation'], 'unknown')

    def test_the_real_filing_writers_read_back_as_themselves(self):
        # The issue's evidence: a shadow filing and its label lines were read as unknown.
        filing._audit_confirmed(self.loop, self.loop['repo'], 12, 'a' * 40, 'file_shadow_issue', 'x')
        self.assertEqual((self.latest()['role'], self.latest()['operation']),
                         ('reader', 'file_shadow_issue'))
        filing._audit_confirmed(self.loop, self.loop['repo'], 12, 'a' * 40,
                                'shadow_issue_labels_missing', 'x')
        self.assertEqual(self.latest()['operation'], 'shadow_issue_labels_missing')


if __name__ == '__main__':
    unittest.main()
