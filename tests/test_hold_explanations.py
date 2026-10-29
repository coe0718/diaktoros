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
from diaktoros.run_supervisor import views
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
                mock.patch.object(uuid, 'uuid4', return_value=uuid.UUID(int=1)):
            self.sup.enqueue('delivery', self.loop['repo'], 7, 'a' * 40, 'reviewer')

    def row(self, state='waiting', error='held: private /home/private ghp_SECRET', due=1234):
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET state=?,error=?,retry_at=?', (state, error, due))
        rows = views.read_only_view(self.db, self.loop['repo'], 7)
        self.assertTrue(rows)
        return rows[0]

    def test_real_ledger_cli_and_desktop_share_explanation(self):
        row = self.row()
        explanation = views.hold_explanation(row)
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
                result = views.hold_explanation(self.row(error=error))
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
                    result = views.hold_explanation(row)
                    self.assertEqual(result['reason_code'], 'external_write_guard')
                    self.assertEqual(result['operator_action'], 'inspect')
                    self.assertEqual(result['automatic_resume'], 'never')
                    self.assertIsNone(result['next_eligible_start'])
                    self.assertNotIn('retry', result['text'].lower())
                    self.assertNotIn('SECRET', json.dumps(result))
                    guidance = views.next_step(row)
                    self.assertNotIn('re-arm:', guidance)
                    self.assertNotIn('hermes dk retry', guidance)
                    self.assertNotIn('starts', guidance)

    def test_unknown_or_malformed_evidence_does_not_become_permission(self):
        base = self.row()
        for change in ({'state': 'new_state'}, {'state': []}, {'state': {}}, {'write': 'possibly written'},
                       {'retry_at': float('nan')}, {'retry_at': -1},
                       {'retry_at': True}, {'retry_at': 1e100}):
            with self.subTest(change=change):
                result = views.hold_explanation(dict(base, **change))
                self.assertIsNone(result['next_eligible_start'])
                self.assertEqual(result['operator_action'], 'inspect')
        missing = dict(base)
        del missing['write']
        self.assertEqual(views.hold_explanation(missing)['reason_code'], 'unknown')
        redacted = views.hold_explanation(dict(base, id='/private/token', head='ghp_SECRET'))
        self.assertIsNone(redacted['run_id'])
        self.assertIsNone(redacted['head'])
        self.assertNotIn('SECRET', json.dumps(redacted))
        for state, reason in [('failed', 'run_failed'), ('cancelled', 'run_cancelled')]:
            result = views.hold_explanation(dict(base, state=state, write=None))
            self.assertEqual(result['reason_code'], reason)
            self.assertEqual(result['automatic_resume'], 'unknown')

    def test_real_write_ahead_receipt_overrides_a_waiting_state(self):
        row = self.row()
        with ledger.connect(self.db) as con:
            con.execute('INSERT INTO review_receipts(run_id,state,generation,principal_id,created) '
                        'VALUES(?,?,?,?,?)', (row['id'], 'pending', 'generation', 42, 1))
        rows = views.read_only_view(self.db, self.loop['repo'], 7)
        self.assertTrue(rows)
        result = views.hold_explanation(rows[0])
        self.assertEqual(result['reason_code'], 'external_write_guard')
        self.assertEqual(result['automatic_resume'], 'never')
        self.assertIsNone(result['next_eligible_start'])
        self.assertNotIn('pending', result['text'])
        # Error prose cannot override durable evidence, even when it resembles a recoverable hold.
        for state in ('waiting', 'failed', 'cancelled', 'uncertain'):
            guarded = self.row(state=state, error=run_supervisor.POLICY_CANCELLATIONS[0])
            self.assertIsNotNone(guarded['write'])
            advice = views.next_step(guarded, 'widgets')
            self.assertNotIn('hermes dk retry', advice)
            self.assertNotIn('no external write', advice)
            self.assertNotIn('hermes dk retry', self.desktop_view()['stuck'][0]['next'])

    def test_backend_contract_fixture_matrix(self):
        cases = {'run_waiting': self.row(), 'run_failed': self.row(state='failed'),
                 'run_cancelled': self.row(state='cancelled', error=run_supervisor.POLICY_CANCELLATIONS[0]),
                 'external_write_guard': self.row(state='uncertain')}
        cases['unknown'] = dict(cases['run_waiting'], state='future_state')
        result = {name: views.hold_explanation(row) for name, row in cases.items()}
        fixture = pathlib.Path(__file__).parent / 'fixtures/desktop_hold_explanations.json'
        self.assertEqual(result, json.loads(fixture.read_text()))

    def desktop_view(self, db=None):
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=db or self.db):
            return desktop_api.pr_view('widgets', 7)

    def test_missing_ledger_is_unavailable_but_successfully_read_empty_is_available(self):
        missing = pathlib.Path(self.temp.name) / 'missing.sqlite'
        view = self.desktop_view(missing)
        self.assertIs(view['ledger_available'], False)
        self.assertEqual(view['stuck'], [])
        self.assertFalse(missing.exists())
        with ledger.connect(self.db) as con:
            con.execute('DELETE FROM runs')
        view = self.desktop_view()
        self.assertIs(view['ledger_available'], True)
        self.assertEqual(view['stuck'], [])
        self.assertEqual(view['turns_total'], 0)

    def test_real_ledger_facts_do_not_duplicate_or_override_host_advice(self):
        for state in ('waiting', 'failed', 'cancelled', 'uncertain'):
            error = (run_supervisor.POLICY_CANCELLATIONS[0] if state == 'cancelled'
                     else 'held: private /home/private ghp_SECRET')
            with self.subTest(state=state):
                row = self.row(state=state, error=error)
                stuck = self.desktop_view()['stuck'][0]
                self.assertEqual(stuck['next'], views.next_step(row, 'widgets'))
                text = stuck['explanation']['text']
                self.assertNotIn('Automatic resume:', text)
                self.assertNotIn('Operator action:', text)
                for advice in ('inspect ', 'never replay', 'hermes dk retry', 'starts'):
                    self.assertNotIn(advice, text.lower())
                if state == 'waiting':
                    self.assertIn('recorded earliest recheck', stuck['next'])
                    self.assertNotIn('starts', stuck['next'])
                elif state == 'failed':
                    self.assertIn('re-arm: hermes dk retry', stuck['next'])
                elif state == 'cancelled':
                    self.assertIn('re-admit it:', stuck['next'])
                else:
                    self.assertIn('reconcile DB', stuck['next'])
                    self.assertNotIn('hermes dk retry', stuck['next'])

    def test_waiting_backoff_and_policy_hold_only_offer_a_recorded_recheck(self):
        for error in ('held: provider window', 'transient worker error'):
            with self.subTest(error=error):
                row = self.row(error=error)
                with mock.patch.object(run_supervisor.time, 'time', return_value=1200):
                    advice = views.next_step(row)
                self.assertIn('recorded earliest recheck', advice)
                self.assertIn('34s', advice)
                self.assertNotIn('starts', advice)
                self.assertNotIn('waits for the reset', advice)
                self.assertIn('host eligibility', advice)

    def test_real_malformed_head_is_withheld_without_suppressing_the_row(self):
        self.row()
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET head=?', ('ghp_SECRET',))
        view = self.desktop_view()
        self.assertIs(view['ledger_available'], True)
        self.assertEqual(len(view['stuck']), 1)
        self.assertIsNone(view['stuck'][0]['explanation']['head'])
        self.assertEqual(view['stuck'][0]['head'], '')
        self.assertEqual(view['turns'][0]['head'], '')
        self.assertNotIn('ghp_SEC', json.dumps(view))

    def test_production_pr_route_missing_and_unreadable_ledger(self):
        import importlib.util
        from hermes_prereqs import skip_or_fail
        for module in ('fastapi', 'httpx'):
            if importlib.util.find_spec(module) is None:
                skip_or_fail(self, module + ' is not installed')
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        root = pathlib.Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('hold_explanations_api', root / 'dashboard/plugin_api.py')
        assert spec is not None and spec.loader is not None
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        app = FastAPI()
        app.include_router(api.router, prefix='/api/plugins/diaktoros')
        missing = pathlib.Path(self.temp.name) / 'missing.sqlite'
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=missing), \
                TestClient(app, raise_server_exceptions=False) as client:
            response = client.get('/api/plugins/diaktoros/pr?loop=widgets&pr=7')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIs(response.json()['ledger_available'], False)
            actual = response.json()
            actual['read_at'] = 1800000000  # freeze only the response's volatile read timestamp
            fixture = pathlib.Path(__file__).parent / 'fixtures/desktop_missing_ledger_pr.json'
            self.assertEqual(actual, json.loads(fixture.read_text()))
            self.assertFalse(missing.exists())
            missing.write_bytes(b'not a SQLite database')
            response = client.get('/api/plugins/diaktoros/pr?loop=widgets&pr=7')
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json(), {'detail': 'Desktop read unavailable; retry the read.'})

    def test_presentation_reads_do_not_modify_artifacts_or_start_work(self):
        row = self.row()
        before = self.db.read_bytes()
        with mock.patch.object(self.sup, '_spawn', side_effect=AssertionError('spawn')):
            for _ in range(3):
                views.hold_explanation(row)
        self.assertEqual(before, self.db.read_bytes())


if __name__ == '__main__':
    unittest.main()
