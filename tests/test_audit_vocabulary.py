"""The broker audit's roles and operations, pinned to every writer and to both readers.

#2293 named every audit role; #2404: the operations a writer records read back as themselves on
the Desktop, not ``unknown``. #2473: the drift guard sees every writer, not only literal roles.
A role or operation that reaches an audit line through a variable is followed to where it comes
from: an assignment, the callers that pass it (across modules), or a guard that refuses anything
outside a fixed list before the write (``broker.authorize``'s seats, ``PERFORMED_OPERATIONS``).
A guard counts only where it runs before the write on every path (``_audit_writers``). A value it
cannot follow fails the test, so a new writer can never pass it unseen.
"""
import _home_guard  # noqa: F401
import ast
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from _audit_writers import ROOT, Unresolved, Writers
from diaktoros import broker, config, desktop_api, gh
from diaktoros.broker import filing


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


def synthetic(source):
    return Writers([('synthetic.py', source)]).written()


_AUDIT = "    _audit(loop, 'acme/w', 1, '', '', role, 'review', 'x')\n"
_GUARD = "if role not in ('reviewer',):\n{i}    raise ValueError(role)\n"
_CHECK = "def check(*, role):\n{early}    " + _GUARD.format(i='    ') + "\n"
# A writer whose role comes from its caller, which passes one no list names. Each guard here
# can be skipped before the write, so it bounds nothing: the caller's role is what is recorded.
SKIPPABLE = {
    'under if False': "    if False:\n        " + _GUARD.format(i='        ') + _AUDIT,
    'after the write': _AUDIT + "    " + _GUARD.format(i='    '),
    'in a try that swallows it': ("    try:\n        " + _GUARD.format(i='        ')
                                  + "    except ValueError:\n        pass\n" + _AUDIT),
    'in a loop': "    for _ in ():\n        " + _GUARD.format(i='        ') + _AUDIT,
    'in a callee past an early return': "    check(role=role)\n" + _AUDIT,
    'in a callee called conditionally': "    loop and check(role=role)\n" + _AUDIT,
    'in a callee another of its name lacks': "    check(role=role)\n" + _AUDIT,
    'in a generator callee': "    check(role=role)\n" + _AUDIT,
    'in an async callee': "    check(role=role)\n" + _AUDIT,
    'in a decorated callee': "    check(role=role)\n" + _AUDIT,
    'rebound after it': "    " + _GUARD.format(i='    ') + "    role = 'intruder'\n" + _AUDIT,
}
_CHECKS = {'in a callee past an early return':
           _CHECK.format(early="    if role == 'intruder':\n        return\n"),
           # A call runs neither body: it makes an iterator or a coroutine (second review).
           'in a generator callee': _CHECK.format(early='').replace("\n\n", "\n    yield\n\n"),
           'in an async callee': "async " + _CHECK.format(early=''),
           'in a decorated callee': "@functools.cache\n" + _CHECK.format(early=''),
           'in a callee another of its name lacks':
           _CHECK.format(early='') + "def check(*, role):\n    pass\n\n"}
_CALLER = "\ndef call(loop):\n    write(loop, 'intruder')\n"


class SyntheticWriters(unittest.TestCase):
    """The drift guard (``Writers.written``, which the vocabulary assertion reads) on writers
    written here, so a regression in it fails a test of its own."""

    def test_a_variable_role_reaches_the_vocabulary_and_a_refused_one_does_not(self):
        # #2473: the literal-only guard saw no role here at all, so 'intruder' passed unseen.
        roles, operations = synthetic(
            "def write(loop, role, operation):\n"
            "    _audit(loop, 'acme/w', 1, '', '', role, operation, 'x')\n\n"
            "def bounded(loop, *, role):\n    " + _GUARD.format(i='    ') + _AUDIT +
            "\ndef callers(loop):\n    write(loop, 'intruder', 'push')\n"
            "    bounded(loop, role='excluded')\n")
        self.assertEqual(roles, {'intruder', 'reviewer'})
        self.assertEqual(operations, {'push', 'review'})
        self.assertEqual(roles - set(broker.AUDIT_ROLES), {'intruder'})

    def test_a_guard_that_can_be_skipped_bounds_nothing(self):
        for shape, body in SKIPPABLE.items():
            with self.subTest(shape=shape):
                check = _CHECKS.get(shape, _CHECK.format(early=''))
                roles, _ = synthetic(check + "def write(loop, role):\n" + body + _CALLER)
                self.assertEqual(roles, {'intruder'})

    def test_a_generator_guard_fails_the_full_vocabulary(self):
        # Second review on #2587: beside every real writer, an unrun generator guard must not
        # hide the role its writer records from the vocabulary assertion.
        sources = [(str(path), path.read_text())
                   for path in sorted((ROOT / 'diaktoros').rglob('*.py'))]
        sources.append(('synthetic.py', _CHECK.format(early='').replace(
            "def check(", "def check_gen_2587(").replace("\n\n", "\n    yield\n\n")
            + "def write_gen_2587(loop, role):\n    check_gen_2587(role=role)\n" + _AUDIT
            + "\ndef call_gen_2587(loop):\n    write_gen_2587(loop, 'intruder')\n"))
        roles, _ = Writers(sources).written()
        self.assertIn('intruder', roles)
        self.assertNotEqual(roles, set(broker.AUDIT_ROLES))

    def test_a_guard_before_the_write_on_every_path_bounds_it(self):
        for body in ("    " + _GUARD.format(i='    ') + _AUDIT, "    check(role=role)\n" + _AUDIT,
                     "    try:\n        check(role=role)\n    except ValueError:\n        raise\n"
                     + _AUDIT):
            with self.subTest(body=body):
                roles, _ = synthetic(_CHECK.format(early='') + "def write(loop, role):\n"
                                     + body + _CALLER)
                self.assertEqual(roles, {'reviewer'})


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
