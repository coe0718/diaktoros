"""#2637: the audit writers refuse a role or operation outside the vocabulary at run time.

The static drift guard (``tests/_audit_writers.py``) reads ordinary code; code written to evade it
can always find another spelling. So ``broker.audit_vocabulary`` checks every line as it is
written, in ``broker._audit`` and ``safe_push._audit`` alike, and nothing reaches the file. A
foreign value already in the file (an older line, a hand edit) still reads as ``unknown``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from diaktoros import broker, config, desktop_api, gh, safe_push


class TheWritersRefuseForeignValues(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.loop = {'id': 'widgets', 'repo': 'acme/widgets',
                     'state_dir': str(Path(temp.name) / 'state')}
        config.state_dir(self.loop).mkdir()
        self.path = config.state_dir(self.loop) / 'broker-audit.jsonl'
        for patcher in (patch.object(config, 'readable_loops', return_value=([self.loop], [])),
                        patch.object(gh, 'api', side_effect=AssertionError('network forbidden'))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()] \
            if self.path.exists() else []

    def broker_line(self, role, operation):
        broker._audit(self.loop, 'acme/widgets', 12, 'a' * 40, '', role, operation, 'x')

    def test_broker_audit_refuses_a_foreign_role_or_operation(self):
        for role, operation in (('intruder', 'review'), ('reader', 'exfiltrate'),
                                (None, 'review'), ('reader', None)):
            with self.subTest(role=role, operation=operation), \
                    self.assertRaisesRegex(broker.BrokerDenied, 'not in the audit vocabulary'):
                self.broker_line(role, operation)
        self.assertEqual(self.lines(), [])
        self.broker_line('reader', 'review')                     # control: a known pair is written
        self.assertEqual([(r['role'], r['operation']) for r in self.lines()], [('reader', 'review')])

    def test_safe_push_audit_refuses_a_foreign_or_missing_role(self):
        for record in ({'role': 'intruder', 'operation': 'push'}, {'operation': 'push'},
                       {'role': 'fixer', 'operation': 'force_push'}):
            with self.subTest(record=record), \
                    self.assertRaisesRegex(broker.BrokerDenied, 'not in the audit vocabulary'):
                safe_push._audit(self.loop, record)
        self.assertEqual(self.lines(), [])
        safe_push._audit(self.loop, {'role': 'fixer', 'operation': 'push'})       # control
        self.assertEqual([r['role'] for r in self.lines()], ['fixer'])

    def test_a_foreign_role_already_in_the_file_reads_unknown(self):
        self.broker_line('reader', 'review')
        with self.path.open('a') as out:
            out.write(json.dumps({**self.lines()[0], 'role': '/home/private',
                                  'at': time.time() + 1}) + '\n')
        self.assertEqual(desktop_api.records_view('widgets')['broker']['items'][0]['role'],
                         'unknown')


if __name__ == '__main__':
    unittest.main()
