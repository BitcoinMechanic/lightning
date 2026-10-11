#!/usr/bin/env python3
"""Authority fault boundaries with private durable journals; no daemon claim."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bounded_authority as ba
import chain_identity as ci
import held_gate as hg
import route_plan as rp
from test_route_plan import Fake, NOW, AMOUNT, PREIMAGE, pin


def bounds(kind='outgoing', count=2):
    value = dict(max_swaps=count, max_amount_msat=3000000, total_amount_msat=6000000)
    value.update(dict(max_fee_msat=10000, max_delay_blocks=288, max_hops=4) if kind == 'outgoing'
                 else dict(min_cltv_delta=40, max_cltv_delta=80))
    return value


class Node(Fake):
    def __init__(self, profile=ci.BTC):
        super().__init__(profile)
        self.history_rows, self.gate = [], None
        self.fail_reply = None
        self.effective_resolutions = 0

    def __call__(self, method, **params):
        if method in ('getinfo', 'decode', 'listpeerchannels', 'listsendpays', 'getroutes'):
            return super().__call__(method, **params)
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'listhtlcs': return dict(htlcs=copy.deepcopy(self.history_rows))
        if method == 'sendpay':
            self.payments = [self.attempt(self.plan_value)]
            self.history_rows = [self.htlc('out', 'SENT_ADD_ACK_REVOCATION')]
            self.rows[0]['htlcs'] = copy.deepcopy(self.history_rows)
            result = dict(status='pending')
        elif method == 'swap-gate-register':
            if self.gate is None:
                self.quote = copy.deepcopy(params['quote'])
                self.gate = dict(payment_hash=self.quote['payment_hash'], chain=self.chain, phase='quoted', binding=None,
                    cltv_expiry=None, amount_msat=self.quote['amount_msat'], hook_ready=False, needs_attention=False,
                    terminal_is_intent=True)
            else: assert self.quote == params['quote']
            result = dict(registered=True)
        elif method == 'swap-gate-status': return copy.deepcopy(self.gate)
        elif method in ('swap-gate-release', 'swap-gate-fail'):
            phase = 'release_intent' if method.endswith('release') else 'fail_intent'
            assert params['binding'] == self.gate['binding']
            assert self.gate['phase'] in ('held', phase)
            if self.gate['phase'] == 'held': self.effective_resolutions += 1
            self.gate.update(phase=phase, hook_ready=False)
            result = dict(recorded=phase)
        else: raise AssertionError('unexpected method '+method)
        if self.fail_reply == method:
            self.fail_reply = None
            raise TimeoutError('lost reply')
        return result

    def htlc(self, direction, state):
        return dict(short_channel_id=pin(self.profile)['short_channel_id'], id=3, expiry=555,
                    direction=direction, amount_msat=2002000 if direction == 'out' else AMOUNT,
                    payment_hash=self.decoded['payment_hash'], created_index=11, state=state)

    def hold(self):
        self.gate.update(phase='held', binding=dict(channel=pin(self.profile), htlc_id=3), cltv_expiry=555, hook_ready=True)
        self.history_rows = [self.htlc('in', 'RCVD_ADD_ACK_REVOCATION')]
        self.rows[0]['htlcs'] = copy.deepcopy(self.history_rows)

    def finish(self, incoming=False, failed=False):
        self.rows[0]['htlcs'] = []
        self.history_rows[0]['state'] = 'SENT_REMOVE_ACK_REVOCATION' if incoming else 'RCVD_REMOVE_ACK_REVOCATION'
        if not incoming:
            self.payments[0]['status'] = 'failed' if failed else 'complete'
            if not failed: self.payments[0]['payment_preimage'] = PREIMAGE

    def quote_data(self):
        return dict(chain=self.chain, payment_hash=self.decoded['payment_hash'], payment_secret=self.decoded['payment_secret'],
                    amount_msat=AMOUNT, expires_at=NOW+1200, min_cltv_delta=40, max_cltv_delta=80,
                    approved_channels=[pin(self.profile)])


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'authority'; self.node = Node(); self.now = NOW
        self.journal = None
        self.open(fresh=True)
        self.addCleanup(lambda: self.journal.close())

    def open(self, fresh=False):
        if self.journal: self.journal.close()
        if fresh: ba.initialize_fixture(self.path, self.node.chain)
        self.journal = hg.Journal(self.path)
        self.auth = ba.Authority(self.journal, self.node.chain, self.node, clock=lambda:self.now)

    def enroll(self, kind='outgoing', limit=None):
        gid = self.auth.create_grant(kind, [pin(self.node.profile)], limit or bounds(kind), NOW+3600, confirmed=True)
        data = self.node.plan() if kind == 'outgoing' else self.node.quote_data()
        self.node.plan_value = data
        eid = self.auth.enroll(gid, data, confirmed=True)
        return gid, eid, data

    def writes(self, method): return sum(m == method for m,p in self.node.calls)

    def test_both_protocols_exact_lost_send_recovery(self):
        for profile in (ci.BTC, ci.PRIVKEYIO_XBT):
            if profile != self.node.profile:
                self.path = Path(self.tmp.name)/'xbt'; self.node = Node(profile); self.open(fresh=True)
            gid, eid, data = self.enroll()
            self.node.fail_reply = 'sendpay'
            with self.assertRaises(TimeoutError): self.auth.send(eid)
            self.open(); self.auth.pause(gid); self.now = NOW+5000
            self.assertEqual(self.auth.enroll(gid, data, confirmed=True), eid)
            self.assertEqual(self.auth.send(eid)['state'], 'pending')
            attempt = copy.deepcopy(self.auth.record['entries'][eid]['attempt'])
            self.open(); self.node.finish(); self.assertEqual(self.auth.reconcile(eid)['state'], 'complete')
            self.assertTrue(self.auth.status(eid)['terminal'])
            self.assertEqual(self.auth.record['entries'][eid]['attempt']['identity'], attempt['identity'])
            self.auth.send(eid); self.assertEqual(self.writes('sendpay'), 1)
            self.assertFalse(any(m == 'getroutes' for m,p in self.node.calls[self.node.calls.index(next(c for c in self.node.calls if c[0] == 'sendpay')):]))
            self.now = NOW

    def test_crash_before_send_and_zero_rows_never_resend(self):
        gid, eid, data = self.enroll()
        original = self.auth.update
        def crash(eid, entry): original(eid, entry); raise RuntimeError('crash before rpc')
        with patch.object(self.auth, 'update', side_effect=crash), self.assertRaises(RuntimeError): self.auth.send(eid)
        self.open()
        for _ in range(3): self.assertEqual(self.auth.send(eid)['state'], 'send_uncertain')
        self.assertEqual(self.writes('sendpay'), 0)
        with self.assertRaises(ValueError): self.auth.retire(eid, confirmed=True)

    def test_ambiguous_enrollment_replace_is_atomic_and_idempotent(self):
        gid = self.auth.create_grant('outgoing', [pin()], bounds(), NOW+3600, confirmed=True)
        data = self.node.plan(); original = self.journal.write
        def after(record): original(record); raise OSError('reply lost after replace')
        with patch.object(self.journal, 'write', side_effect=after), self.assertRaises(hg.StorageError):
            self.auth.enroll(gid, data, confirmed=True)
        self.assertTrue(self.auth.status()['blocked']); self.assertEqual(self.writes('sendpay'), 0)
        self.open(); eid = self.auth.enroll(gid, data, confirmed=True)
        self.assertEqual(len(self.auth.record['entries']), 1)
        self.assertEqual(self.auth.record['entries'][eid]['payload'], data)

    def test_failed_storage_before_replace_cannot_send(self):
        gid, eid, data = self.enroll()
        with patch.object(self.journal, 'write', side_effect=OSError('disk')), self.assertRaises(hg.StorageError):
            self.auth.send(eid)
        with self.assertRaises(ValueError): self.auth.send(eid)
        self.assertEqual(self.writes('sendpay'), 0)
        self.open(); self.assertNotIn('send', self.auth.record['entries'][eid])

    def test_unknown_attempt_changed_identity_and_outcome_regression(self):
        gid, eid, data = self.enroll(); self.auth.send(eid)
        original = copy.deepcopy(self.node.payments)
        for variant in ('missing', 'extra', 'group', 'created_index', 'preimage'):
            self.node.payments = copy.deepcopy(original)
            if variant == 'missing': self.node.payments = []
            elif variant == 'extra': self.node.payments *= 2
            elif variant == 'group': self.node.payments[0]['groupid'] = 2
            elif variant == 'created_index': self.node.payments[0]['created_index'] += 1
            else: self.node.payments[0].update(status='complete', payment_preimage='00'*32)
            with self.subTest(variant=variant), self.assertRaises(ValueError): self.auth.reconcile(eid)
        self.node.payments = original; self.node.finish(); self.auth.reconcile(eid)
        self.node.payments[0].pop('payment_preimage'); self.node.payments[0]['status'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'attempt_outcome_regressed'): self.auth.reconcile(eid)
        self.assertEqual(self.writes('sendpay'), 1)

    def test_invoice_changed_or_expired_funding_signing_type_and_pause_before_send(self):
        gid, eid, data = self.enroll()
        cases = [('rows', lambda: self.node.rows[0].update(funding_txid='88'*32)),
                 ('rows', lambda: self.node.rows[0]['channel_type']['bits'].append(514)),
                 ('rows', lambda: self.node.rows[0].update(reestablished=False)),
                 ('rows', lambda: self.node.rows[0].update(spendable_msat=AMOUNT)),
                 ('decoded', lambda: self.node.decoded.update(payment_hash='22'*32)),
                 ('decoded', lambda: self.node.decoded.update(min_final_cltv_expiry=80))]
        for field, change in cases:
            original = copy.deepcopy(getattr(self.node, field)); change()
            with self.assertRaises(ValueError): self.auth.send(eid)
            setattr(self.node, field, original)
        self.now = NOW+5000
        with self.assertRaisesRegex(ValueError, 'grant_closed'): self.auth.send(eid)
        self.now = NOW; self.auth.pause(gid)
        with self.assertRaisesRegex(ValueError, 'grant_closed'): self.auth.send(eid)
        self.assertEqual(self.writes('sendpay'), 0)

    def test_limits_count_fee_in_debit_and_no_refund_on_failure(self):
        gid, eid, data = self.enroll(limit=dict(bounds(), total_amount_msat=4003999))
        self.auth.send(eid); self.node.finish(failed=True); self.auth.reconcile(eid)
        self.node.decoded['payment_hash'] = '11'*32
        data2 = self.node.plan()
        with self.assertRaisesRegex(ValueError, 'grant_budget_exhausted'): self.auth.enroll(gid, data2, confirmed=True)
        self.assertEqual(len(self.auth.record['entries']), 1)

    def test_new_grant_and_other_enrollment_require_previous_terminal_history(self):
        gid, eid, data = self.enroll()
        with self.assertRaisesRegex(ValueError, 'previous_enrollment_requires_recovery'):
            self.auth.create_grant('incoming', [pin()], bounds('incoming'), NOW+3600, confirmed=True)
        self.auth.send(eid); self.node.payments[0].update(status='complete', payment_preimage=PREIMAGE)
        self.assertFalse(self.auth.reconcile(eid)['terminal'])
        self.node.rows[0]['htlcs'] = []
        self.assertFalse(self.auth.reconcile(eid)['terminal'])
        self.node.finish(); self.assertTrue(self.auth.reconcile(eid)['terminal'])
        self.node.history_rows = []
        with self.assertRaisesRegex(ValueError, 'original_htlc_missing'):
            self.auth.create_grant('incoming', [pin()], bounds('incoming'), NOW+3600, confirmed=True)

    def test_incoming_binding_gate_intent_and_terminal_history(self):
        gid, eid, data = self.enroll('incoming')
        self.node.fail_reply = 'swap-gate-register'
        with self.assertRaises(TimeoutError): self.auth.publish(eid)
        self.open(); self.auth.publish(eid); self.node.hold(); self.auth.reconcile(eid)
        bound = copy.deepcopy(self.auth.record['entries'][eid]['incoming'])
        self.auth.pause(gid); self.now = NOW+5000; self.open()
        self.node.fail_reply = 'swap-gate-release'
        with self.assertRaises(TimeoutError): self.auth.resolve(eid, payment_preimage=PREIMAGE)
        self.open(); self.assertFalse(self.auth.reconcile(eid)['terminal'])
        self.auth.resolve(eid, payment_preimage=PREIMAGE)
        self.assertEqual(self.node.effective_resolutions, 1)
        self.node.finish(incoming=True)
        self.assertEqual(self.auth.reconcile(eid)['state'], 'released')
        self.assertTrue(self.auth.status(eid)['terminal'])
        self.assertEqual(self.auth.record['entries'][eid]['incoming'], bound)
        with self.assertRaisesRegex(ValueError, 'resolution_changed'): self.auth.resolve(eid)

    def test_incoming_rebind_trimmed_and_unowned_decision_refused(self):
        gid, eid, data = self.enroll('incoming'); self.auth.publish(eid); self.node.hold(); self.auth.reconcile(eid)
        original = copy.deepcopy(self.node.gate)
        for change in ({'binding':dict(channel=pin(), htlc_id=4)}, {'cltv_expiry':556},
                       {'phase':'release_intent'}, {'needs_attention':True}):
            self.node.gate = dict(original, **change)
            with self.assertRaises(ValueError): self.auth.reconcile(eid)
        self.node.gate = original; self.node.rows[0]['htlcs'][0]['local_trimmed'] = True
        with self.assertRaisesRegex(ValueError, 'incoming_not_committed'): self.auth.resolve(eid)
        self.assertEqual(self.writes('swap-gate-fail'), 0)

    def test_expired_unpaid_and_expired_unsent_retirement(self):
        gid, eid, data = self.enroll('incoming'); self.auth.publish(eid); self.now = data['expires_at']
        self.assertEqual(self.auth.reconcile(eid)['state'], 'expired')
        self.now -= 1
        with self.assertRaisesRegex(ValueError, 'terminal_evidence_regressed'): self.auth.reconcile(eid)
        self.now = NOW+1500
        gid2 = self.auth.create_grant('outgoing', [pin()], bounds(), self.now+3600, confirmed=True)
        self.node.decoded.update(payment_hash='11'*32, created_at=self.now, expiry=1800)
        data2 = rp.plan(self.node, self.node.chain, 'lnbcrt1synthetic-decoded-by-fake', [pin()],
                        {k: bounds()[k] for k in rp.LIMIT_KEYS}, clock=lambda:self.now)
        eid2 = self.auth.enroll(gid2, data2, confirmed=True); self.now = data2['expires_at']
        self.auth.retire(eid2, confirmed=True)
        with self.assertRaisesRegex(ValueError, 'enrollment_terminal'): self.auth.send(eid2)

    def test_restore_marker_all_operations_blocked_and_status_redacted(self):
        gid, eid, data = self.enroll(); self.auth.send(eid)
        (self.path/'restored.json').write_text('{}')
        self.open()
        for operation in (lambda:self.auth.send(eid), lambda:self.auth.reconcile(eid), lambda:self.auth.pause(gid),
                          lambda:self.auth.enroll(gid, data, confirmed=True)):
            with self.assertRaisesRegex(ValueError, 'restored_authority_blocked'): operation()
        status = self.auth.status(eid); self.assertTrue(status['blocked'])
        raw = json.dumps(status)
        for field in ('invoice', 'payment_hash', 'payment_secret', 'preimage', 'channel_id'): self.assertNotIn(field, raw)

    def test_single_writer_legacy_journal_and_cross_protocol_refused(self):
        with self.assertRaises(BlockingIOError): hg.Journal(self.path)
        value = self.journal.read(); value['format'] = 'startos-forward-routed-v1'
        self.journal.write(value)
        with self.assertRaisesRegex(ValueError, 'authority_format_refused'): self.open()
        self.journal.close()
        self.node = Node(ci.PRIVKEYIO_XBT)
        with self.assertRaises(ValueError): self.open()

    def test_exact_schema_bool_budget_pin_and_enrollment_identity(self):
        gid, eid, data = self.enroll()
        for mutate in (lambda r:r.update(schema=True),
                       lambda r:r['entries'][eid].update(send=1),
                       lambda r:r['entries'][eid]['payload']['route'][0].update(delay=201),
                       lambda r:r['grants'][gid]['terms']['limits'].update(max_swaps=True),
                       lambda r:r['grants'][gid].update(paused=1)):
            value = copy.deepcopy(self.auth.record); mutate(value)
            with self.assertRaises(ValueError): ba.validate(value, self.node.chain)

    def test_no_confirmation_no_authority_and_snapshot_is_detached(self):
        with self.assertRaisesRegex(ValueError, 'confirmation_required'):
            self.auth.create_grant('outgoing', [pin()], bounds(), NOW+3600)
        gid, eid, data = self.enroll(); data['route'][0]['delay'] = 201
        self.assertNotEqual(data, self.auth.record['entries'][eid]['payload'])
        with self.assertRaisesRegex(ValueError, 'confirmation_required'): self.auth.enroll(gid, data)


if __name__ == '__main__': unittest.main()
