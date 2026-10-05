"""Adversarial offline host receipt checks; never contacts GitHub."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from review_loop import broker, broker_ipc, gh, ledger, review_receipt
from review_loop.run_supervisor import Supervisor

HEAD = 'a' * 40
BASE = 'b' * 40
REPO = 'acme/widgets'


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        tokens = {}
        for login in ('read', 'review', 'fix'):
            p = root / (login + '.pat')
            p.write_text('dummy-' + login)
            tokens[login] = str(p)
        self.loop = {'repo': REPO, 'base': 'main', 'state_dir': str(root),
                     'tokens': tokens, 'read_token': 'read', 'reviewer_seat': 'review',
                     'seats': {'reviewer': {'login': 'review'}, 'fixer': {'login': 'fix'}}}
        self.pr = {'number': 7, 'state': 'open', 'draft': False,
                   'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': REPO}},
                   'head': {'ref': 'fix-7', 'sha': HEAD, 'repo': {'full_name': REPO}}}
        self.sup = Supervisor(root / 'runs.sqlite')
        self.sup.enqueue('d', REPO, 7, HEAD, 'reviewer')
        self.generation = review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='running', generation=?", (self.generation,))
            self.run_id = con.execute('SELECT id FROM runs').fetchone()[0]
        self.scope = broker_ipc.RunScope(REPO, 7, HEAD, 'reviewer', 'fix-7',
                                         self.run_id, str(self.sup.db), self.generation)
        self.ledger = review_receipt.ReceiptLedger(str(self.sup.db), self.run_id,
                                                    self.generation)
        self.posts = 0
        self.mode = None

    def api(self, loop, path, method='GET', body=None, login=None):
        if path == '/user':
            return {'login': login, 'id': {'read': 1, 'review': 2, 'fix': 3}[login]}
        if path == f'/repos/{REPO}/pulls/7':
            if self.mode == 'stale-before-post' and self.posts == 0:
                pr = copy.deepcopy(self.pr)
                pr['base']['sha'] = 'c' * 40
                return pr
            if self.mode == 'stale-after-post' and self.posts:
                pr = copy.deepcopy(self.pr)
                pr['base']['sha'] = 'c' * 40
                return pr
            # The PR leaving the eligible set while the write is in flight.
            # Separate modes: `generation_for` refuses state and draft the same
            # way, but they are not the same event -- a draft can be marked
            # ready again and a close cannot.
            if self.mode == 'closed-after-post' and self.posts:
                pr = copy.deepcopy(self.pr)
                pr['state'] = 'closed'
                return pr
            if self.mode == 'retarget-after-post' and self.posts:
                pr = copy.deepcopy(self.pr)
                pr['base']['ref'] = 'develop'
                return pr
            if self.mode == 'closed-before-post' and not self.posts:
                pr = copy.deepcopy(self.pr)
                pr['state'] = 'closed'
                return pr
            if self.mode == 'draft-after-post' and self.posts:
                pr = copy.deepcopy(self.pr)
                pr['draft'] = True
                return pr
            return self.pr
        if path == f'/repos/{REPO}/pulls/7/reviews' and method == 'POST':
            self.posts += 1
            self.assertEqual(body['commit_id'], HEAD)
            if self.mode == 'lost-post':
                raise TimeoutError('response lost')
            return {'id': 19}
        if path == f'/repos/{REPO}/pulls/7/reviews/19':
            if self.mode == 'readback-failed':
                raise TimeoutError('exact review unreadable')
            return {'id': 20 if self.mode == 'wrong-id' else 19,
                    'state': 'APPROVED', 'commit_id': HEAD,
                    'user': {'id': 2, 'login': 'review'}}
        raise AssertionError(path)

    def receipt(self):
        with ledger.connect(self.sup.db) as con:
            return con.execute('SELECT state,review_id FROM review_receipts').fetchone()

    def test_exact_id_confirmed_and_replay_rejected(self):
        with mock.patch.object(gh, 'api', side_effect=self.api):
            self.assertEqual(review_receipt.submit(self.loop, self.scope, self.ledger,
                                                   'APPROVE', 'reviewed'), {'id': 19})
            with self.assertRaises(sqlite3.IntegrityError):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'again')
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.receipt(), ('confirmed', 19))

    def test_stale_generation_before_post_denies_without_claim(self):
        self.mode = 'stale-before-post'
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises(review_receipt.ReceiptDenied):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'reviewed')
        self.assertEqual(self.posts, 0)
        self.assertIsNone(self.receipt())

    def test_exact_id_mismatch_stays_uncertain(self):
        self.mode = 'wrong-id'
        self.assert_uncertain_after_post()

    def test_failed_exact_id_readback_stays_uncertain(self):
        self.mode = 'readback-failed'
        self.assert_uncertain_after_post()

    def test_generation_change_after_post_stays_uncertain(self):
        self.mode = 'stale-after-post'
        self.assert_uncertain_after_post()

    def test_lost_post_stays_uncertain_without_retry(self):
        self.mode = 'lost-post'
        self.assert_uncertain_after_post()

    # -- the PR leaving the eligible set while the write is in flight --------
    #
    # `generation_for` refuses a PR that is not `open` and not non-draft, and
    # `submit` re-reads through it *after* the POST. Nothing exercised that arm
    # through `submit` until now: every mode above varies the base SHA, the
    # readback id, the readback result or a POST timeout -- never `state` or
    # `draft`, so the check could be deleted and the suite stayed green.
    #
    # The POST cannot be recalled by the time the re-read refuses, which is why
    # these assert the raises rather than trying to assert the write away. They
    # deliberately do **not** pin the durable receipt: naming that outcome is
    # #154, and a test that pinned today's value would have to be rewritten by
    # it. Three separate causes, so CI names the one that broke.

    def deny_during_the_write(self, mode):
        """Drive one window case; return the reason the write was refused for."""
        self.mode = mode
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises(review_receipt.ReceiptDenied) as caught:
                review_receipt.submit(self.loop, self.scope, self.ledger,
                                      'APPROVE', 'reviewed')
        self.assertEqual(self.posts, 1, 'the review reached GitHub before the re-read')
        reason = str(caught.exception)
        self.assertTrue(reason.strip(), 'a refusal must name its own cause')
        return reason

    def assert_ineligible(self, mode, reason):
        got = self.deny_during_the_write(mode)
        self.assertEqual(got, 'reviewed_pr_ineligible: ' + reason)
        self.assertEqual(self.receipt(), ('posted', 19))
        # Final: a replay is refused and nothing is posted again.
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises((sqlite3.IntegrityError, review_receipt.ReceiptDenied, broker.BrokerDenied)):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'again')
        self.assertEqual(self.posts, 1)
        # The supervisor records it and retry refuses the run.
        self.sup.record_review_ineligible(self.run_id, reason)
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET state='failed' WHERE id=?", (self.run_id,))
        with self.assertRaisesRegex(ValueError, 'never replayed'):
            self.sup.retry(self.run_id)

    def test_a_pr_that_closes_during_the_write_is_named(self):
        self.assert_ineligible('closed-after-post', 'closed')

    def test_a_pr_that_goes_draft_during_the_write_is_named(self):
        self.assert_ineligible('draft-after-post', 'draft')

    def test_a_pr_retargeted_during_the_write_is_named(self):
        self.assert_ineligible('retarget-after-post', 'retargeted to develop')

    def test_unknown_state_before_the_write_keeps_the_old_error(self):
        self.mode = 'closed-before-post'
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaisesRegex(broker.BrokerDenied,
                                        'PR identity, state or draft status changed') as c:
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'reviewed')
        self.assertNotIsInstance(c.exception, review_receipt.ReviewedPrIneligible)
        self.assertEqual(self.posts, 0)
        self.assertIsNone(self.receipt())

    def test_a_base_sha_move_after_the_write_is_not_ineligible(self):
        self.deny_during_the_write('stale-after-post')
        self.assertEqual(self.receipt(), ('claimed', None))

    def test_a_pr_retargeted_during_the_write_is_refused(self):
        self.deny_during_the_write('stale-after-post')

    def test_the_two_causes_of_a_post_write_refusal_are_distinguishable(self):
        """A base retarget and an ineligible PR are not the same event.

        One is benign -- the PR is re-queued against its new base. The other
        means a verdict is now sitting on a PR that can no longer receive it.
        Whoever reconciles the run has to be able to tell them apart, and that
        must not depend on the receipt: both currently end up claimed, which is
        the gap in #154. Whether a close and a draft also separate is #154's
        call; this pins the pair that already do.
        """
        retarget = self.deny_during_the_write('stale-after-post')
        self.setUp()
        ineligible = self.deny_during_the_write('closed-after-post')
        self.assertNotEqual(retarget, ineligible)

    def assert_uncertain_after_post(self):
        with mock.patch.object(gh, 'api', side_effect=self.api):
            with self.assertRaises((review_receipt.ReceiptDenied, TimeoutError)):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'reviewed')
            with self.assertRaises((sqlite3.IntegrityError, review_receipt.ReceiptDenied, broker.BrokerDenied)):
                review_receipt.submit(self.loop, self.scope, self.ledger, 'APPROVE', 'again')
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.receipt(), ('claimed', None))
        self.sup.complete_uncertain(self.run_id, '', 1, 'failed')
        # The real owner is required to complete; a replay cannot forge ownership.
        with ledger.connect(self.sup.db) as con:
            con.execute("UPDATE runs SET owner='owner' WHERE id=?", (self.run_id,))
        self.sup.complete_uncertain(self.run_id, 'owner', 1, 'failed')
        with ledger.connect(self.sup.db) as con:
            self.assertEqual(con.execute('SELECT state FROM runs').fetchone()[0], 'uncertain')

    def test_missing_base_sha_cannot_pin_generation(self):
        self.pr['base'].pop('sha')
        with self.assertRaises(review_receipt.ReceiptDenied):
            review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
