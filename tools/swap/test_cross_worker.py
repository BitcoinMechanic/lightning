#!/usr/bin/env python3
"""Durability and cross-chain decisions with synthetic remote evidence."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bounded_authority as ba
import cross_contract as cc
import cross_worker as cw
import customer_invoice as invoice
import held_gate as hg
from test_cross_contract import example
from test_route_plan import NOW


class Remote:
    def __init__(self, direction='forward'):
        self.c,self.obs = example(direction); self.entries = {}; self.calls = []; self.lost = None
        self.before_send = lambda:None; self.height = 100; self.channel_ready = True
        # The plan fixture's preimage is supplied here solely as test data.
        from test_route_plan import PREIMAGE
        self.preimage = PREIMAGE

    def status(self,kind):
        e = self.entries[kind]
        state = e.get('terminal') or (e.get('attempt',{}).get('state','send_uncertain') if 'send' in e
            else 'resolution_intent' if 'decision' in e else 'held' if 'incoming' in e
            else 'waiting_for_payment' if e.get('publication') == 'registered' else 'enrolled')
        return dict(kind=kind,state=state,terminal='terminal' in e,regtest_only=True,blocked=False)

    def hold(self):
        c = self.c; q = c['incoming']; p = q['approved_channels'][0]; e = self.entries['incoming']
        e['incoming'] = dict(binding=dict(channel=copy.deepcopy(p),htlc_id=3),expiry=474)
        e['htlc'] = dict(short_channel_id=p['short_channel_id'],id=3,expiry=474,direction='in',
            amount_msat=q['amount_msat'],payment_hash=q['payment_hash'],created_index=7)

    def complete(self, failed=False, terminal=True):
        e = self.entries['outgoing']; e['attempt']['state'] = 'failed' if failed else 'complete'
        if not failed: e['attempt']['preimage'] = self.preimage
        if terminal: e['terminal'] = e['attempt']['state']

    def __call__(self,kind,gid,op,body):
        self.calls.append((kind,op,copy.deepcopy(body)))
        assert gid == ba.digest(self.c['grants'][kind])
        if op == 'inspect':
            result = copy.deepcopy(self.obs[kind]); result['blockheight'] = self.height if kind=='incoming' else 900000
            if body.get('entry_id'):
                assert body['entry_id'] == cw.enrollment(self.c,kind)
                result.pop('hints',None); result['entry_id'] = body['entry_id']; result['channel_ready'] = self.channel_ready
        elif op == 'enroll':
            assert ba.equal(body['data'],self.c[kind]) and body['confirmation'] == ba.digest(self.c[kind])
            self.entries.setdefault(kind,dict(grant_id=gid,payload=copy.deepcopy(body['data'])))
            result = dict(entry_id=cw.enrollment(self.c,kind))
        elif op == 'publish':
            self.entries[kind]['publication'] = 'registered'; result = dict(registered=True)
        elif op == 'invoice':
            result = dict(format=invoice.FORMAT,entry_id=cw.enrollment(self.c,kind),
                invoice='lnbcrt1'+'a'*150,expires_at=self.c['pay_until'])
        elif op == 'evidence':
            result = dict(entry_id=cw.enrollment(self.c,kind),entry=copy.deepcopy(self.entries[kind]),status=self.status(kind))
        elif op == 'send':
            self.before_send(); e = self.entries[kind]; assert 'send' not in e
            p = self.c['outgoing']; e['send'] = True
            identity = dict(payment_hash=p['payment_hash'],groupid=1,partid=0,destination=p['recipient'],
                amount_msat=p['amount_msat'],amount_sent_msat=p['route'][0]['amount_msat'],created_at=NOW,created_index=10)
            e['attempt'] = dict(state='pending',identity=identity)
            e['htlc'] = dict(short_channel_id=p['channel']['short_channel_id'],id=5,expiry=900200,direction='out',
                amount_msat=p['route'][0]['amount_msat'],payment_hash=p['payment_hash'],created_index=11)
            result = self.status(kind)
        elif op in ('release','fail'):
            e = self.entries[kind]; decision = dict(action=op)
            if op == 'release':
                ba.preimage(body['preimage'],self.c['incoming']['payment_hash']); decision['preimage'] = body['preimage']
            assert 'decision' not in e or e['decision'] == decision
            e['decision'] = decision; e['terminal'] = 'released' if op=='release' else 'failed'; result = self.status(kind)
        elif op == 'retire':
            assert 'send' not in self.entries[kind]
            self.entries[kind]['terminal'] = 'retired'; result = self.status(kind)
        else: raise AssertionError(op)
        if self.lost == (kind,op): self.lost = None; raise TimeoutError('synthetic reply loss')
        return copy.deepcopy(result)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'worker'; cw.initialize_fixture(self.root)
        self.remote = Remote(); self.now = NOW; self.journal = None; self.open(); self.addCleanup(self.close)

    def close(self):
        if self.journal: self.journal.close()

    def open(self):
        self.close(); self.journal = hg.Journal(self.root); self.w = cw.Worker(self.journal,self.remote,lambda:self.now)

    def setup_swap(self):
        self.cid = self.w.prepare(self.remote.c); self.w.confirm(self.cid,self.cid)
        for _ in range(3): self.w.step(self.cid); self.open()
        self.assertEqual(self.w.status(self.cid)['state'],'waiting_for_payment')

    def finish(self):
        for _ in range(4): self.open(); self.w.step(self.cid)

    def sends(self): return sum(op=='send' for _,op,_ in self.remote.calls)

    def test_both_directions_require_durable_confirmation_and_held_payment(self):
        for direction in ('forward','reverse'):
            if direction=='reverse':
                self.close(); self.root = Path(self.tmp.name)/'reverse'; cw.initialize_fixture(self.root)
                self.remote = Remote(direction); self.open()
            self.cid = self.w.prepare(self.remote.c)
            for _ in range(2): self.w.step(self.cid)
            self.assertFalse(any(op=='enroll' for _,op,_ in self.remote.calls))
            with self.assertRaises(ValueError): self.w.confirm(self.cid,'0'*64)
            self.w.confirm(self.cid,self.cid); self.open()
            for _ in range(3): self.w.step(self.cid)
            self.w.step(self.cid); self.assertEqual(self.sends(),0)
            self.remote.hold()
            def check_disk():
                r = cw.validate(self.journal.read())['swaps'][self.cid]
                self.assertEqual(r['approval']['digest'],self.cid); self.assertIn('send',r)
                self.assertEqual(r['send']['incoming']['entry']['incoming'],self.remote.entries['incoming']['incoming'])
            self.remote.before_send = check_disk
            self.w.step(self.cid); self.remote.complete(); self.finish()
            self.assertEqual(self.w.status(self.cid)['state'],'settled'); self.assertEqual(self.sends(),1)

    def test_lost_enrollment_publication_invoice_send_and_release_replies(self):
        self.cid = self.w.prepare(self.remote.c); self.w.confirm(self.cid,self.cid)
        for kind,op in [('outgoing','enroll'),('incoming','enroll'),('incoming','publish'),('incoming','invoice')]:
            self.remote.lost = (kind,op)
            with self.assertRaises(TimeoutError): self.w.step(self.cid)
            self.open()
            if op != 'publish': self.w.step(self.cid)
        saved = self.w.payment_invoice(self.cid); self.remote.hold(); self.remote.lost = ('outgoing','send')
        with self.assertRaises(TimeoutError): self.w.step(self.cid)
        self.open(); self.w.step(self.cid); self.assertEqual(self.sends(),1)
        self.remote.complete(); self.w.step(self.cid); self.remote.lost = ('incoming','release')
        with self.assertRaises(TimeoutError): self.w.step(self.cid)
        self.finish(); self.assertEqual(self.w.status(self.cid)['state'],'settled')
        self.assertEqual(self.w.payment_invoice(self.cid),saved); self.assertEqual(self.sends(),1)

    def test_unknown_send_and_transport_error_never_release_fail_or_resend(self):
        self.setup_swap(); self.remote.hold()
        real = self.remote
        def drop(kind,gid,op,body):
            if op=='send': raise TimeoutError('request lost before node received it')
            return real(kind,gid,op,body)
        self.w.call = drop
        with self.assertRaises(TimeoutError): self.w.step(self.cid)
        for _ in range(3): self.open(); self.w.step(self.cid)
        self.assertEqual(self.sends(),0); self.assertEqual(self.w.status(self.cid)['state'],'outgoing_uncertain')
        with self.assertRaises(ValueError): self.w.cancel(self.cid,self.cid)
        self.assertFalse(any(op in ('release','fail','retire') for _,op,_ in self.remote.calls))

    def test_pause_expiry_and_changed_market_do_not_replace_enrolled_recovery(self):
        self.setup_swap(); self.remote.hold(); self.w.step(self.cid)
        original = copy.deepcopy(self.w.record['swaps'][self.cid]['contract'])
        self.now += 10000
        for o in self.remote.obs.values(): o['info']['paused'] = True
        self.remote.complete(); self.finish()
        self.assertEqual(self.w.status(self.cid)['state'],'settled')
        self.assertEqual(self.w.record['swaps'][self.cid]['contract'],original)
        self.assertEqual(self.sends(),1)

    def test_outgoing_failure_requires_terminal_history_and_fails_incoming_once(self):
        self.setup_swap(); self.remote.hold(); self.w.step(self.cid); self.remote.complete(failed=True,terminal=False)
        self.w.step(self.cid); self.assertNotIn('decision',self.w.record['swaps'][self.cid])
        self.remote.entries['outgoing']['terminal'] = 'failed'; self.w.step(self.cid)
        self.remote.lost = ('incoming','fail')
        with self.assertRaises(TimeoutError): self.w.step(self.cid)
        self.finish(); self.assertEqual(self.w.status(self.cid)['state'],'failed'); self.assertEqual(self.sends(),1)

    def test_attempt_preimage_binding_and_outcome_changes_refuse(self):
        self.setup_swap(); self.remote.hold(); self.w.step(self.cid); self.w.step(self.cid)
        original = copy.deepcopy(self.remote.entries)
        changes = [lambda r:r['incoming']['incoming']['binding'].update(htlc_id=4),
            lambda r:r['outgoing']['attempt']['identity'].update(created_index=12),
            lambda r:r['outgoing'].pop('htlc'),
            lambda r:r['outgoing'].update(attempt=dict(r['outgoing']['attempt'],state='complete',preimage='0'*64))]
        for change in changes:
            self.remote.entries = copy.deepcopy(original); change(self.remote.entries); self.open()
            with self.assertRaises(ValueError): self.w.step(self.cid)
            self.assertNotIn('decision',self.w.record['swaps'][self.cid])
        self.remote.entries = original
        self.remote.complete(); self.w.step(self.cid)
        self.remote.entries['outgoing']['attempt']['state'] = 'pending'
        with self.assertRaises(ValueError): self.w.step(self.cid)

    def test_held_inspection_reserve_timing_grant_checks_precede_send(self):
        self.setup_swap(); self.remote.hold()
        baseline = copy.deepcopy(self.remote.obs)
        for change in [lambda o:o['outgoing'].update(reserve_msat=1),
            lambda o:o['incoming'].update(checked_at=NOW-31),
            lambda o:o['incoming']['info'].update(paused=True),
            lambda o:o['outgoing']['info']['grant'].update(nonce='99'*32)]:
            self.remote.obs = copy.deepcopy(baseline); change(self.remote.obs)
            with self.assertRaises(ValueError): self.w.step(self.cid)
            self.assertEqual(self.sends(),0)
        self.remote.obs = baseline; self.remote.height = 125
        with self.assertRaises(ValueError): self.w.step(self.cid)
        self.assertEqual(self.sends(),0)
        self.remote.height = 100; self.remote.channel_ready = False
        with self.assertRaisesRegex(ValueError,'contract_channel_not_ready'): self.w.step(self.cid)
        self.assertEqual(self.sends(),0)

    def test_expired_held_quote_fails_without_send_then_retires_original_plan(self):
        self.setup_swap(); self.remote.hold(); self.now = self.remote.c['pay_until']
        self.w.step(self.cid); self.w.step(self.cid)
        self.assertEqual(self.sends(),0)
        self.now = self.remote.c['outgoing']['expires_at']; self.finish()
        self.assertEqual(self.w.status(self.cid)['state'],'failed')

    def test_unpaid_expiry_retires_only_after_recipient_expiry(self):
        self.setup_swap(); self.remote.entries['incoming']['terminal'] = 'expired'
        self.now = self.remote.c['pay_until']; self.w.step(self.cid)
        self.assertEqual(self.w.status(self.cid)['state'],'waiting_for_retirement')
        self.assertFalse(any(op=='retire' for _,op,_ in self.remote.calls))
        self.now = self.remote.c['outgoing']['expires_at']; self.finish()
        self.assertEqual(self.w.status(self.cid)['state'],'expired'); self.assertEqual(self.sends(),0)

    def test_storage_uncertainty_before_send_prevents_send_and_freezes_instance(self):
        self.setup_swap(); self.remote.hold(); write = self.journal.write
        def fail(record):
            if 'send' in record['swaps'][self.cid]:
                write(record); raise OSError('lost fsync result')
            write(record)
        with patch.object(self.journal,'write',side_effect=fail), self.assertRaises(hg.StorageError): self.w.step(self.cid)
        self.assertEqual(self.sends(),0)
        with self.assertRaises(ValueError): self.w.step(self.cid)
        self.open(); self.w.step(self.cid)
        self.assertEqual(self.w.status(self.cid)['state'],'outgoing_uncertain'); self.assertEqual(self.sends(),0)

    def test_approval_write_failure_precedes_all_remote_mutations(self):
        self.cid = self.w.prepare(self.remote.c)
        with patch.object(self.journal,'write',side_effect=OSError('disk')),self.assertRaises(hg.StorageError):
            self.w.confirm(self.cid,self.cid)
        self.open(); self.w.step(self.cid)
        self.assertEqual(self.w.status(self.cid)['state'],'ready_for_confirmation')
        self.assertFalse(any(op=='enroll' for _,op,_ in self.remote.calls))

    def test_single_writer_restore_and_redacted_status(self):
        self.setup_swap()
        with self.assertRaises(BlockingIOError): hg.Journal(self.root)
        (self.root/'restored.json').write_text('{}')
        self.assertTrue(self.w.status(self.cid)['blocked'])
        for op in (lambda:self.w.step(self.cid),lambda:self.w.payment_invoice(self.cid),lambda:self.w.confirm(self.cid,self.cid)):
            with self.assertRaises(ValueError): op()
        text = str(self.w.status(self.cid))
        for private in (self.cid,self.remote.c['outgoing']['payment_hash'],self.remote.c['incoming']['payment_secret']):
            self.assertNotIn(private,text)

    def test_contract_and_unowned_remote_mutation_refuse(self):
        self.setup_swap(); saved = copy.deepcopy(self.w.record)
        changed = copy.deepcopy(saved); changed['swaps'][self.cid]['contract']['pricing']['markup_bps'] = 100
        with self.assertRaises(ValueError): cw.validate(changed)
        self.remote.hold(); self.remote('outgoing',ba.digest(self.remote.c['grants']['outgoing']),'send',{})
        with self.assertRaisesRegex(ValueError,'worker_unowned_send'): self.w.step(self.cid)


if __name__ == '__main__': unittest.main()
