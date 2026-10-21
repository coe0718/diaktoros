"""The first #674 slice: ledger facts, not a second gate-policy evaluator."""
import _home_guard  # noqa: F401
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import uuid
from unittest import mock

from diaktoros import config, desktop_api, ledger, run_supervisor
from diaktoros.cli import operations


class LedgerExplanations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / 'runs.sqlite'
        self.sup = run_supervisor.Supervisor(self.db)
        self.loop = {'id': 'widgets', 'repo': 'owner/widgets', 'cap': 3,
                     'state_dir': str(pathlib.Path(self.temp.name) / 'state')}
        with mock.patch.object(self.sup, '_spawn'), \
                mock.patch.object(run_supervisor.uuid, 'uuid4', return_value=uuid.UUID(int=1)):
            self.sup.enqueue('delivery', self.loop['repo'], 7, 'a' * 40, 'reviewer')

    def row(self, state='waiting', error='held: private /home/private ghp_SECRET', due=1234):
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET state=?,error=?,retry_at=?', (state, error, due))
        rows = run_supervisor.read_only_view(self.db, self.loop['repo'], 7)
        self.assertTrue(rows)
        return rows[0]

    def test_real_ledger_cli_and_desktop_share_explanation(self):
        row = self.row()
        explanation = run_supervisor.hold_explanation(row)
        fixture = pathlib.Path(__file__).parent / 'fixtures/desktop_hold_explanations.json'
        self.assertEqual(explanation, json.loads(fixture.read_text())['run_waiting'])
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db), \
                mock.patch.object(operations, '_ledger_path', return_value=self.db):
            view = desktop_api.pr_view('widgets', 7)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                operations._print_ledger_runs(self.loop, 7, 'run: ', 10)
        self.assertEqual(view['stuck'][0]['explanation'], explanation)
        self.assertIn(explanation['text'], out.getvalue())
        self.assertEqual(explanation['run_id'], row['id'])
        self.assertEqual(explanation['head'], 'a' * 40)
        self.assertEqual(explanation['evidence_source'], 'run_ledger')
        self.assertEqual(explanation['completeness'], 'partial')
        self.assertEqual(explanation['next_eligible_start'], 1234)
        self.assertNotIn('PRIVATE', json.dumps(explanation).upper())
        self.assertNotIn('ghp_', json.dumps(explanation))

    def test_pacing_ci_and_caps_remain_partial_without_structured_policy_reason(self):
        for error in ('held: waiting for CI', 'held: reviewer usage window (provider)',
                      'held: reviewer daily turn cap reached', 'held: human path',
                      'held: isolated worker unavailable', 'held: conflict', 'held: escalation'):
            with self.subTest(error=error):
                result = run_supervisor.hold_explanation(self.row(error=error))
                self.assertEqual(result['reason_code'], 'run_waiting')
                self.assertEqual(result['waiting_for'], 'host_recheck')
                self.assertEqual(result['automatic_resume'], 'unknown')
                self.assertEqual(result['operator_action'], 'inspect')
                self.assertNotIn(error, result['text'])

    def test_uncertain_or_write_evidence_never_offers_replay_or_a_start(self):
        for state in ('waiting', 'failed', 'uncertain', 'cancelled'):
            for write in (None, 'private /home/private ghp_SECRET'):
                row = dict(self.row(), state=state, write=write)
                if state == 'cancelled':
                    row['error'] = run_supervisor.POLICY_CANCELLATIONS[0]
                if state != 'uncertain' and write is None:
                    continue
                with self.subTest(state=state, write=write):
                    result = run_supervisor.hold_explanation(row)
                    self.assertEqual(result['reason_code'], 'external_write_guard')
                    self.assertEqual(result['operator_action'], 'inspect')
                    self.assertEqual(result['automatic_resume'], 'never')
                    self.assertIsNone(result['next_eligible_start'])
                    self.assertNotIn('retry', result['text'].lower())
                    self.assertNotIn('SECRET', json.dumps(result))
                    guidance = run_supervisor.next_step(row)
                    self.assertNotIn('re-arm:', guidance)
                    self.assertNotIn('hermes dk retry', guidance)
                    self.assertNotIn('starts', guidance)

    def test_unknown_or_malformed_evidence_does_not_become_permission(self):
        base = self.row()
        for change in ({'state': 'new_state'}, {'state': []}, {'state': {}}, {'write': 'possibly written'},
                       {'retry_at': float('nan')}, {'retry_at': -1},
                       {'retry_at': True}, {'retry_at': 1e100}):
            with self.subTest(change=change):
                result = run_supervisor.hold_explanation(dict(base, **change))
                self.assertIsNone(result['next_eligible_start'])
                self.assertEqual(result['operator_action'], 'inspect')
        missing = dict(base)
        del missing['write']
        self.assertEqual(run_supervisor.hold_explanation(missing)['reason_code'], 'unknown')
        redacted = run_supervisor.hold_explanation(dict(base, id='/private/token', head='ghp_SECRET'))
        self.assertIsNone(redacted['run_id'])
        self.assertIsNone(redacted['head'])
        self.assertNotIn('SECRET', json.dumps(redacted))
        for state, reason in [('failed', 'run_failed'), ('cancelled', 'run_cancelled')]:
            result = run_supervisor.hold_explanation(dict(base, state=state, write=None))
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(result['automatic_resume'], 'unknown')

    def test_real_write_ahead_receipt_overrides_a_waiting_state(self):
        row = self.row()
        with ledger.connect(self.db) as con:
            con.execute('INSERT INTO review_receipts(run_id,state,generation,principal_id,created) '
                        'VALUES(?,?,?,?,?)', (row['id'], 'pending', 'generation', 42, 1))
        rows = run_supervisor.read_only_view(self.db, self.loop['repo'], 7)
        self.assertTrue(rows)
        result = run_supervisor.hold_explanation(rows[0])
        self.assertEqual(result['reason_code'], 'external_write_guard')
        self.assertEqual(result['automatic_resume'], 'never')
        self.assertIsNone(result['next_eligible_start'])
        self.assertNotIn('pending', result['text'])

    def test_backend_contract_fixture_matrix(self):
        cases = {'run_waiting': self.row(), 'run_failed': self.row(state='failed'),
                 'run_cancelled': self.row(state='cancelled', error=run_supervisor.POLICY_CANCELLATIONS[0]),
                 'external_write_guard': self.row(state='uncertain')}
        cases['unknown'] = dict(cases['run_waiting'], state='future_state')
        result = {name: run_supervisor.hold_explanation(row) for name, row in cases.items()}
        fixture = pathlib.Path(__file__).parent / 'fixtures/desktop_hold_explanations.json'
        self.assertEqual(result, json.loads(fixture.read_text()))

    def test_presentation_reads_do_not_modify_artifacts_or_start_work(self):
        row = self.row()
        before = self.db.read_bytes()
        with mock.patch.object(self.sup, '_spawn', side_effect=AssertionError('spawn')):
            for _ in range(3):
                run_supervisor.hold_explanation(row)
        self.assertEqual(before, self.db.read_bytes())


if __name__ == '__main__':
    unittest.main()
