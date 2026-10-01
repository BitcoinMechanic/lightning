"""Customer-only review, durable intent, read-only recovery and bounded payment."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import reverse_customer as customer
from swap_controller import save

A, B, C = ['02'+f'{i:064x}' for i in range(1, 4)]


class CustomerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root/'payment'
        self.preimage = '11'*32
        self.hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.offer = dict(format=customer.FORMAT, btc_invoice_sha256=hashlib.sha256(b'lnbc-original').hexdigest(),
                          xbt_invoice='lnxbt-offer', btc_sats=1500, xbt_sats=350000, expires_at=1300)
        common = dict(valid=True, type='bolt11 invoice', payment_hash=self.hash,
                      created_at=900, expiry=1000, features='024100', min_final_cltv_expiry=622)
        self.decoded = {'lnbc-original': dict(common, currency='bc', amount_msat=1500000, payee=C),
                        'lnxbt-offer': dict(common, currency='xbt', amount_msat=350000000, payee=B)}
        self.rows = []
        self.calls = []
        self.fail_send = False
        self.record_send = True
        self.node_id = A
        self.balance = 400000000

    def rpc(self, cli, method, *args):
        self.assertEqual(cli[0], '/customer', 'only customer wallet RPC allowed')
        self.calls.append(method)
        if method == 'getinfo':
            return dict(network='xbt', id=self.node_id)
        if method == 'decode':
            return self.decoded[args[0]]
        if method == 'listpeerchannels':
            return {'channels': [dict(peer_id=B, state='CHANNELD_NORMAL', peer_connected=True,
                                      htlcs=[], spendable_msat=self.balance)]}
        if method == 'listpays':
            self.assertEqual(args, ('payment_hash='+self.hash,))
            return {'pays': self.rows}
        if method == 'pay':
            self.assertEqual(json.loads((self.directory/'customer.json').read_text())['phase'], 'submitted')
            self.assertEqual(args, ('bolt11=lnxbt-offer', 'maxfee=0msat', 'maxdelay=2016', 'retry_for=0'))
            if self.record_send:
                self.rows = [dict(payment_hash=self.hash, bolt11='lnxbt-offer', destination=B,
                    amount_msat=350000000, amount_sent_msat=350000000, status='pending')]
            if self.fail_send:
                raise subprocess.TimeoutExpired('PRIVATE', 20)
            return {}
        raise AssertionError(method)

    def review(self):
        return customer.review(self.offer, 'lnbc-original', ['/customer'], self.directory,
                               400000, rpc=self.rpc, now=lambda: 1000)

    def pay(self):
        return customer.pay(self.directory, rpc=self.rpc, now=lambda: 1000)

    def test_review_has_no_mutations_or_operator_paths(self):
        result = self.review()
        self.assertFalse(result['payment_started'])
        self.assertNotIn('pay', self.calls)
        self.assertEqual((self.directory/'customer.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('payment_hash', result)

    def test_substituted_invoice_amount_hash_currency_and_delay_refused(self):
        original = copy.deepcopy(self.decoded)
        for key, value in (('amount_msat', 350001000), ('payment_hash', 'aa'*32),
                           ('currency', 'bc'), ('min_final_cltv_expiry', 2017), ('valid', False)):
            self.decoded = copy.deepcopy(original)
            self.decoded['lnxbt-offer'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.review()
            self.assertFalse(self.directory.exists())
        self.assertNotIn('pay', self.calls)

    def test_caps_and_expiry_refused_before_rpc(self):
        for key, value in (('xbt_sats', 400001), ('expires_at', 1000),
                           ('btc_invoice_sha256', 'aa'*32), ('btc_sats', 1501)):
            original = self.offer.copy()
            self.offer[key] = value
            with self.assertRaises(ValueError):
                self.review()
            self.offer = original
        self.assertEqual(self.calls, [])

    def test_pending_then_complete_proof_and_repeated_pay_never_resends(self):
        self.review()
        self.assertEqual(self.pay()['outcome'], 'pending')
        self.assertEqual(self.pay()['outcome'], 'pending')
        self.rows[0].update(status='complete', preimage=self.preimage)
        result = self.pay()
        self.assertEqual(result['outcome'], 'complete')
        self.assertTrue(result['matching_preimage_verified'])
        self.assertEqual(result['xbt_sent_sats'], 350000)
        self.assertEqual(self.calls.count('pay'), 1)
        self.assertNotIn(self.preimage, json.dumps(result))

    def test_missing_record_after_lost_reply_never_resends(self):
        self.review()
        self.fail_send = True
        self.record_send = False
        self.assertEqual(self.pay()['outcome'], 'unknown')
        self.assertEqual(self.pay()['outcome'], 'unknown')
        self.assertEqual(self.calls.count('pay'), 1)

    def test_failed_attempt_is_not_retried(self):
        self.review()
        self.pay()
        self.rows[0]['status'] = 'failed'
        self.assertEqual(self.pay()['outcome'], 'failed')
        self.assertEqual(self.calls.count('pay'), 1)

    def test_existing_payment_before_submission_is_refused(self):
        self.review()
        self.rows = [{'payment_hash': self.hash}]
        with self.assertRaises(ValueError):
            self.pay()
        self.assertNotIn('pay', self.calls)
        self.assertEqual(json.loads((self.directory/'customer.json').read_text())['phase'], 'reviewed')

    def test_expired_quote_before_submission_leaves_reviewed_state(self):
        self.review()
        with self.assertRaises(ValueError):
            customer.pay(self.directory, rpc=self.rpc, now=lambda: 1300)
        self.assertNotIn('pay', self.calls)

    def test_changed_wallet_and_bad_proof_refused(self):
        self.review()
        self.node_id = C
        with self.assertRaises(ValueError):
            self.pay()
        self.node_id = A
        self.pay()
        self.rows[0].update(status='complete', preimage='22'*32)
        with self.assertRaises(ValueError):
            self.pay()
        self.assertEqual(self.calls.count('pay'), 1)

    def test_competing_submission_refused_before_rpc(self):
        self.review()
        before = list(self.calls)
        with customer.locked(self.directory), self.assertRaises(BlockingIOError):
            self.pay()
        self.assertEqual(self.calls, before)

    def test_export_whitelists_fields_and_refuses_overwrite(self):
        source = self.root/'operator'
        source.mkdir()
        save(source/'reverse-quote.json', dict(config={'btc_cli': ['PRIVATE']},
            terms=dict(btc_invoice='lnbc-original', btc_amount_msat=1500000, xbt_amount_msat=350000000,
                       expires_at=1300, payment_secret='SECRET'), xbt_invoice='lnxbt-offer'))
        target = self.root/'offer.json'
        customer.export(source, target)
        self.assertEqual(json.loads(target.read_text()), self.offer)
        self.assertNotIn('PRIVATE', target.read_text())
        self.assertNotIn('SECRET', target.read_text())
        customer.export(source, target)
        save(target, {'other': True})
        with self.assertRaises(ValueError):
            customer.export(source, target)


if __name__ == '__main__':
    unittest.main()
