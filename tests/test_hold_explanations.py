"""The first #674 slice: ledger facts, not a second gate-policy evaluator."""
import _home_guard  # noqa: F401
import contextlib
import io
import json
import pathlib
import tempfile
import time
import unittest
import uuid
from unittest import mock

from diaktoros import config, desktop_api, ledger, run_supervisor, stats
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

    # Lookalikes of the host writers' lines (#674, review of #2398 F2): each mentions a writer's
    # words but is not its line, so none may be classified. Several are one edit from a real one.
    LOOKALIKES = (
        'held: human path mentions daily turn cap', 'held: waiting for CINDER',
        'held: waiting for CI', 'held: waiting for ci', 'held: waiting for CI checks to finish',
        'held: waiting for CINDER on aaaaaaa — 1 check(s) still running or not reported',
        'held: waiting for CI on AAAAAAA — 1 check(s) still running or not reported',
        'held: waiting for CI on aaaaaa — 1 check(s) still running or not reported',
        'held: waiting for CI on aaaaaaa 1 check(s) still running or not reported',
        'note held: waiting for CI on aaaaaaa — 1 check(s) still running or not reported',
        'held: reviewer daily turn cap reached', 'held: reviewer daily turn cap (3) reached',
        'held: nobody daily turn cap (3) reached — resumes 00:00',
        'held: human path; reviewer daily turn cap (3) reached — resumes 00:00',
        'held: reviewer daily turn cap (three) reached — resumes 00:00',
        'held: reviewer usage window', 'held: human path mentions usage window (x) — resumes 00:00',
        'held: reviewer usage window (openai) reached', 'held: reviewer provider unavailablex — resumes 00:00',
        'held: human path', 'held: isolated worker unavailable', 'held: conflict', 'held: escalation',
        'waiting for CI', 'reviewer daily turn cap reached', 'transient error',
        'held: private /home/private ghp_SECRET')

    def test_unstructured_hold_prose_stays_partial_without_a_policy_reason(self):
        # Only the host writers' exact lines (run_supervisor.waiting_hold) are structured; a
        # held: line no writer produces, or a lookalike, is never classified from its prose.
        for error in self.LOOKALIKES:
            with self.subTest(error=error):
                result = views.hold_explanation(self.row(error=error))
                self.assertEqual(result['reason_code'], 'run_waiting')
                self.assertEqual(result['waiting_for'], 'host_recheck')
                self.assertEqual(result['automatic_resume'], 'unknown')
                self.assertEqual(result['operator_action'], 'inspect')
                self.assertNotIn(error, result['text'])
        # The filter is the writers' structure, not their words: every real line passes it.
        for name, error in self.writer_lines().items():
            with self.subTest(writer=name):
                self.assertEqual(views.hold_explanation(self.row(error=error))['reason_code'],
                                 self.EXPECTED[name][0])
        lines = self.ci_hold_lines()
        for error in (*self.LOOKALIKES, lines.pop('shadow_draft')):
            with self.subTest(error=error):
                self.assertEqual(run_supervisor.waiting_hold(error), '')
                self.assertEqual(views.hold_explanation(self.row(error=error))['reason_code'],
                                 'run_waiting')
        for name, error in lines.items():
            with self.subTest(writer=name):
                self.assertEqual(run_supervisor.waiting_hold(error), 'ci')

    # One line per structured hold, each made by its real writer (#674).
    def writer_lines(self):
        from types import SimpleNamespace
        from diaktoros import pacing
        from diaktoros.run_supervisor import turns
        cap = SimpleNamespace(paced_until=None, error=None)
        turns._cap_held(cap, 'reviewer', 3)
        review_only = SimpleNamespace(paced_until=None, error=None)
        turns._cap_held(review_only, pacing.REVIEW_ONLY_SEAT, 2)

        def ci(fixing=False, red=False, review_only=False, **checks):
            state = SimpleNamespace(**{k: checks.get(k, []) for k in
                                       ('cancelled', 'failed', 'missing', 'pending')})
            return turns._ci_hold_text('a' * 40, state, fixing, red, review_only)
        return {
            'ci_wait': ci(pending=['tests']),
            'ci_missing': ci(missing=['tests']),
            'ci_cancelled': ci(cancelled=['tests']),
            'ci_fixer_first': ci(failed=['tests'], fixing=True),
            'ci_failed_wait': ci(failed=['tests'], red=True, review_only=True),
            'ci_failed_fixer_done': ci(failed=['tests'], red=True),
            'provider_window': pacing.hold_line('reviewer', 'openai-codex', 1234.0),
            'provider_unavailable': pacing.hold_line('reviewer', 'openai-codex', 1234.0,
                                                     pacing.UNAVAILABLE + ' (empty stream)'),
            'daily_cap': cap.error,
            'review_only_cap': review_only.error,
        }

    def ci_hold_lines(self):
        """The CI lines written straight through ``run_supervisor.ci_hold_line``: the reviewer's
        base-conflict hold and the shadow gate's waits (the draft wait is no CI wait)."""
        return {
            'ci_conflict': run_supervisor.ci_hold_line(
                'a' * 40, 'conflicts with main — GitHub runs no CI on it'),
            'shadow_ci_wait': run_supervisor.ci_hold_line('a' * 40, '1 check(s) still running'),
            'shadow_draft': run_supervisor.ci_hold_line('a' * 40, run_supervisor.CI_DRAFT_WAIT),
        }

    # The (reason_code, waiting_for, automatic_resume) each writer's line must explain as. A CI
    # wait ends at its event or CI_WAIT_MAX_S (turns._ci_hold_reason, the shadow gate); a red
    # head's hold has no time limit (#1110), so it is 'after_event', and so is a fixer's red head,
    # where only the fixer's turn order ends at the limit (review of #2398, F3).
    CI_TIMED = ('ci_wait', 'ci_checks_or_new_head', 'after_event_or_timeout')
    CI_FIXER = ('ci_fixer_first_wait', 'failed_checks_pass_or_new_head', 'after_event')
    CI_RED = ('ci_failed_wait', 'failed_checks_pass_or_new_head', 'after_event')
    WINDOW = ('provider_window', 'provider_window_reset', 'after_recorded_time')
    CAP = ('daily_cap', 'daily_cap_reset', 'after_recorded_time')
    EXPECTED = {'ci_wait': CI_TIMED, 'ci_missing': CI_TIMED, 'ci_cancelled': CI_TIMED,
                'ci_fixer_first': CI_FIXER, 'ci_conflict': CI_TIMED, 'shadow_ci_wait': CI_TIMED,
                'ci_failed_wait': CI_RED, 'ci_failed_fixer_done': CI_RED,
                'provider_window': WINDOW, 'provider_unavailable': WINDOW,
                'daily_cap': CAP, 'review_only_cap': CAP}

    def structured(self, result) -> tuple:
        return result['reason_code'], result['waiting_for'], result['automatic_resume']

    def test_host_hold_lines_get_their_structured_reason(self):
        lines = self.writer_lines()
        for name, error in lines.items():
            with self.subTest(name=name):
                self.assertEqual(self.structured(views.hold_explanation(self.row(error=error))),
                                 self.EXPECTED[name])
        lines.update(self.ci_hold_lines())
        del lines['shadow_draft']
        self.assertEqual(set(lines), set(self.EXPECTED))
        for name, error in lines.items():
            with self.subTest(name=name):
                result = views.hold_explanation(self.row(error=error, due=1234))
                self.assertEqual(self.structured(result), self.EXPECTED[name])
                self.assertEqual(result['text'], f"{views.HOLD_COPY[result['reason_code']]} "
                                 'Ledger only (partial). Waiting for: '
                                 f"{views.WAITING_FOR_COPY[result['waiting_for']]}. "
                                 'Recorded earliest recheck: 1970-01-01T00:20:34Z.')
                # The time-based holds name their next eligible time; the event-based ones name
                # their event (and keep the recorded recheck, which is when CI is read again).
                self.assertEqual(result['next_eligible_start'], 1234)
                self.assertEqual(result['operator_action'], 'inspect')
                self.assertNotIn(error, result['text'])
        # Each bound is the current wait's, never the whole hold's (review of #2398, F3).
        self.assertIn('this wait ends at the CI wait limit at the latest, but failed required '
                      'checks can still hold it after that, with no time limit',
                      views.HOLD_COPY['ci_wait'])
        self.assertIn('the fixer goes first for at most the CI wait limit; the failed checks still '
                      'hold it after that, with no time limit', views.HOLD_COPY['ci_fixer_first_wait'])
        self.assertIn('with no time limit', views.HOLD_COPY['ci_failed_wait'])
        token = 'held: fixer ' + run_supervisor.DAILY_TOKEN_BUDGET + ' (10) reached — resumes 00:00'
        self.assertEqual(run_supervisor.waiting_hold(token), 'daily_cap')
        self.assertIn('daily_tokens', run_supervisor.hold_remedy('fixer', token))

    def test_a_structured_hold_with_write_evidence_is_never_resumed_or_replayed(self):
        for name, error in self.writer_lines().items():
            # The same line without write evidence is structured: the guard is what overrides it.
            self.assertEqual(self.structured(views.hold_explanation(self.row(error=error))),
                             self.EXPECTED[name], name)
            for state, write in (('waiting', 'push intent'), ('uncertain', None),
                                 ('uncertain', 'push intent')):
                row = dict(self.row(error=error, due=1234), state=state, write=write)
                with self.subTest(name=name, state=state, write=write):
                    result = views.hold_explanation(row)
                    self.assertEqual(self.structured(result),
                                     ('external_write_guard', 'operator_inspection', 'never'))
                    self.assertIsNone(result['next_eligible_start'])
                    self.assertEqual(result['text'], views.hold_explanation(
                        dict(row, error='held: human path'))['text'])
                    advice = views.next_step(row, 'widgets')
                    self.assertIn('inspect', advice)
                    for replay in ('hermes dk retry', '--clear-hold', 'raise the cap', 'recheck'):
                        self.assertNotIn(replay, advice)

    def consumers(self) -> tuple[dict, str]:
        """The Desktop's /pr view and the CLI's printed ledger runs, from the real ledger."""
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db), \
                mock.patch.object(operations, '_ledger_path', return_value=self.db):
            view = desktop_api.pr_view('widgets', 7)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                operations._print_ledger_runs(self.loop, 7, 'run: ', 10)
        return view, out.getvalue()

    def test_structured_holds_reach_cli_and_desktop_identically(self):
        cases = [(error, self.EXPECTED[name]) for name, error in self.writer_lines().items()]
        cases += [(error, ('run_waiting', 'host_recheck', 'unknown')) for error in self.LOOKALIKES[:2]]
        for error, expected in cases:
            with self.subTest(error=error):
                row = self.row(error=error, due=1234)
                view, out = self.consumers()
                explanation = view['stuck'][0]['explanation']
                # Both consumers carry the structured value itself, not merely the same value.
                self.assertEqual(self.structured(explanation), expected)
                self.assertEqual(explanation, views.hold_explanation(row))
                self.assertTrue(explanation['text'].startswith(
                    views.HOLD_COPY[expected[0]] + ' Ledger only (partial). Waiting for: '
                    + views.WAITING_FOR_COPY[expected[1]] + '.'))
                self.assertIn(explanation['text'], out)
                self.assertEqual(view['stuck'][0]['next'], views.next_step(row, 'widgets'))
                self.assertIn(views.next_step(row, 'widgets'), out)
                # A write guard reaches both consumers the same way, whatever the hold line.
                with ledger.connect(self.db) as con:
                    con.execute('UPDATE runs SET state=?', ('uncertain',))
                view, out = self.consumers()
                guarded = view['stuck'][0]['explanation']
                self.assertEqual(self.structured(guarded),
                                 ('external_write_guard', 'operator_inspection', 'never'))
                self.assertIn(guarded['text'], out)
                self.assertNotIn(explanation['text'], out)

    def test_desktop_validator_mirrors_the_host_tables(self):
        # The Desktop rebuilds the text from the same copy and pairings: one host table, no
        # second wording of a reason in the plugin.
        plugin = (pathlib.Path(__file__).resolve().parents[1] / 'desktop/plugin.js').read_text()
        body = plugin[plugin.index('function validHoldExplanation'):plugin.index('function usePr(')]
        for code, copy in views.HOLD_COPY.items():
            self.assertIn(f"{code}: {copy!r}", body)
        for waiting, copy in views.WAITING_FOR_COPY.items():
            self.assertIn(f"{waiting}: {copy!r}", body)
        for code, waiting, resume in views.HOLD_REASONS.values():
            self.assertIn(f"{code}: [{waiting!r}, {resume!r}, true]", body)

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

    def test_next_step_tolerates_a_row_without_a_write_key(self):
        # hold_explanation already accepts such a row; next_step must advise, not KeyError (#1366).
        for state, error in (('cancelled', run_supervisor.POLICY_CANCELLATIONS[0]),
                             ('failed', 'boom'), ('waiting', 'held: x')):
            row = self.row(state=state, error=error)
            del row['write']
            with self.subTest(state=state):
                self.assertIsInstance(views.next_step(row, 'widgets'), str)
        row = self.row(state='cancelled', error=run_supervisor.POLICY_CANCELLATIONS[0])
        del row['write']
        self.assertIn('no external write', views.next_step(row, 'widgets'))

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
        lines = self.writer_lines()
        for name in ('ci_wait', 'ci_failed_wait', 'provider_window', 'daily_cap'):
            cases[name] = self.row(error=lines[name])
        cases['ci_fixer_first_wait'] = self.row(error=lines['ci_fixer_first'])
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

    def test_real_malformed_head_is_withheld_from_now_too(self):
        # /now and /pr answer "which recorded head may be shown" with the same rule (#1459).
        self.row(error='held: provider window')
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET head=?', ('ghp_SECRETTOKEN',))
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db):
            view = desktop_api.now_view('widgets')
        self.assertIs(view['available'], True)
        self.assertEqual(len(view['runs']), 1)
        self.assertEqual(view['runs'][0]['head'], '')
        self.assertFalse(view['runs'][0]['retry']['eligible'])
        self.assertNotIn('ghp_SEC', json.dumps(view))
        stuck = stats.stuck(self.db, self.loop['repo'], time.time())
        self.assertEqual(stuck['head'], '')

    def _now_with_two_heads(self, older: str, newer: str) -> dict:
        """Two waiting reviewer runs for PR 7, the older then the newer one with these recorded
        heads. Returns /now's rows keyed by the (display) head."""
        self.row(error='held: provider window')
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET head=?, created=100, updated=100', (older,))
            con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, error, "
                        "retry_at, turn_key, created, updated) SELECT 'second', 'delivery-2', "
                        "repo, pr, ?, seat, state, error, retry_at, turn_key, 200, 200 FROM runs",
                        (newer,))
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db):
            view = desktop_api.now_view('widgets')
        self.assertEqual(len(view['runs']), 2)
        self.assertNotIn('ghp_SEC', json.dumps(view))
        return {run['head']: run for run in view['runs']}

    def test_malformed_older_head_is_not_eligible_beside_a_newer_valid_row(self):
        # The collision the review found: the withheld head '' must not be a join key. The
        # malformed row gets no retry; the valid newer row keeps its own, naming its own head.
        rows = self._now_with_two_heads('ghp_SECRETTOKEN', 'b' * 40)
        bad, good = rows[''], rows['bbbbbbb']
        self.assertFalse(bad['retry']['eligible'])
        self.assertEqual(bad['retry']['head'], '')
        self.assertTrue(good['retry']['eligible'])
        self.assertEqual(good['retry']['head'], 'b' * 40)

    def test_malformed_newer_head_never_makes_a_valid_row_eligible_or_names_it(self):
        # The malformed run is the ledger's retry target: the valid older row is "not the newest",
        # not eligible, and neither row's retry carries the malformed head or reason.
        rows = self._now_with_two_heads('b' * 40, 'ghp_SECRETTOKEN')
        bad, good = rows[''], rows['bbbbbbb']
        self.assertFalse(bad['retry']['eligible'])
        self.assertEqual(bad['retry']['head'], '')
        self.assertFalse(good['retry']['eligible'])
        self.assertEqual(good['retry']['head'], '')
        self.assertIn('not the newest', good['retry']['reason'])

    def test_empty_recorded_head_beside_a_malformed_one_is_not_eligible(self):
        # The review's second ordering: a genuinely empty head is newer than a malformed one. Both
        # display as '', so a join on the display head made both rows eligible with head ''.
        self.row(error='held: provider window')
        with ledger.connect(self.db) as con:
            con.execute('UPDATE runs SET head=?, created=100, updated=100', ('ghp_SECRETTOKEN',))
            con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, error, "
                        "retry_at, turn_key, created, updated) SELECT 'second', 'delivery-2', "
                        "repo, pr, '', seat, state, error, retry_at, turn_key, 200, 200 FROM runs")
        with mock.patch.object(config, 'readable_loops', return_value=([self.loop], [])), \
                mock.patch.object(run_supervisor, 'production_ledger', return_value=self.db):
            view = desktop_api.now_view('widgets')
        self.assertEqual(len(view['runs']), 2)
        self.assertNotIn('ghp_SEC', json.dumps(view))
        for run in view['runs']:
            self.assertEqual(run['head'], '')
            self.assertFalse(run['retry']['eligible'])
            self.assertEqual(run['retry']['head'], '')

    def test_page_targets_omits_malformed_and_empty_heads_and_keeps_valid_ones(self):
        # The contract of retry_action.page_targets itself, apart from the desktop guard: a head
        # that is not a recorded 40-hex head is never a join key; a valid one keeps its entry.
        from diaktoros import retry_action
        self.row(error='held: provider window')
        with ledger.connect(self.db) as con:
            for n, (pr, head) in enumerate([(8, 'ghp_SECRETTOKEN'), (9, ''), (10, 'c' * 40)]):
                con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, error, "
                            "retry_at, turn_key, created, updated) SELECT ?, ?, repo, ?, ?, seat, "
                            "state, error, retry_at, ?, 300, 300 FROM runs WHERE id != 'x' LIMIT 1",
                            (f'extra{n}', f'delivery-x{n}', pr, head, f'turn-x{n}'))
        con = views.read_only_connect(self.db)
        try:
            found = retry_action.page_targets(con, self.loop['repo'], self.loop)
        finally:
            con.close()
        prs = {key[0]: key for key in found}
        self.assertIn(7, prs)
        self.assertIn(10, prs)
        self.assertNotIn(8, prs)
        self.assertNotIn(9, prs)
        self.assertEqual(found[prs[10]]['head'], 'c' * 40)
        self.assertNotIn('ghp_SEC', json.dumps([list(map(str, k)) + [v] for k, v in found.items()]))

    def test_with_retry_refuses_to_join_an_entry_on_an_empty_head(self):
        # desktop_api._with_retry's own guard: even if the targets hold an entry keyed on '' (a
        # withheld head), a row with no valid head is not joined to it and is never eligible.
        run = {'pr': 7, 'seat': 'reviewer', 'head': '', 'state': 'waiting'}
        entry = {'eligible': True, 'head': '', 'reason': ''}
        with mock.patch.object(desktop_api.retry_action, 'page_targets',
                               return_value={(7, 'reviewer', '', 'waiting'): entry}):
            out = desktop_api._with_retry(self.db, self.loop['repo'], [dict(run)], self.loop)
        self.assertFalse(out[0]['retry']['eligible'])
        self.assertEqual(out[0]['retry']['head'], '')
        self.assertIn('not a valid commit id', out[0]['retry']['reason'])

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
