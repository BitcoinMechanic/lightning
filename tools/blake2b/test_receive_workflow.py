"""Receiving orchestration and repayment replay boundaries; no live RPCs."""
import contextlib
import hashlib
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_market_quotes
from receive_workflow import receive, repay, lock
from swap_controller import save


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = test_market_quotes.MarketTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.receiver = ['receiver']
        self.rows = {}
        self.invoice_calls = 0
        self.directory = self.f.f.root/'receiving'
        self.output = io.StringIO()

    def rpc(self, cli, method, *args):
        if cli == self.receiver:
            if method == 'getinfo':
                return dict(network='xbt', id='receiver')
            if method == 'listinvoices':
                return {'invoices': [self.rows[args[0]]] if args[0] in self.rows else []}
            if method == 'invoice':
                self.invoice_calls += 1
                self.rows[args[1]] = dict(amount_msat=int(args[0][:-4]), status='unpaid',
                                          payment_hash=self.f.f.decoded['payment_hash'], bolt11='lnxbt-market')
                return self.rows[args[1]]
        return self.f.rpc(cli, method, *args)

    def invoke(self, serve):
        with patch('receive_workflow.Lab.rpc', side_effect=self.rpc), \
                patch('market_policy.fetch', side_effect=[self.f.ticker, self.f.book]), \
                patch('swap_service.serve', side_effect=serve), contextlib.redirect_stdout(self.output):
            return receive(self.f.config, self.receiver, self.directory, 350000, threading.Event())

    def test_receive_saves_private_invoice_and_restart_reuses_quote(self):
        self.assertEqual(self.invoke(lambda *a, **k: 0), 0)
        self.assertEqual(self.invoke(lambda *a, **k: 0), 0)
        self.assertEqual(self.invoice_calls, 1)
        self.assertEqual(len(self.f.registered), 1)
        self.assertEqual((self.directory/'btc-invoice.txt').read_text(), 'signed-market\n')
        self.assertEqual((self.directory/'btc-invoice.txt').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.f.f.decoded['payment_hash'], self.output.getvalue())
        self.assertNotIn('signed-market', self.output.getvalue())

    def test_receiver_receipt_verified_automatically(self):
        def settle(directory, stop, report):
            data = json.loads((directory/'quote.json').read_text())
            save(directory/'state.json', dict(data['controller'], phase='btc_released'))
            for row in self.rows.values():
                row.update(status='paid', amount_received_msat=350000000)
            report(dict(event='reconciled', phase='btc_released', payment_hash='PRIVATE'))
            return 0
        self.invoke(settle)
        result = json.loads(self.output.getvalue().splitlines()[-1])
        self.assertTrue(result['receiver_paid'])
        self.assertEqual(result['received_xbt_sats'], 350000)
        self.assertNotIn('PRIVATE', self.output.getvalue())

    def test_lost_invoice_reply_recovers_same_label(self):
        rpc = self.rpc
        def lost(cli, method, *args):
            result = rpc(cli, method, *args)
            if method == 'invoice':
                raise TimeoutError()
            return result
        with patch('receive_workflow.Lab.rpc', side_effect=lost), self.assertRaises(TimeoutError):
            receive(self.f.config, self.receiver, self.directory, 350000, threading.Event())
        self.invoke(lambda *a, **k: 0)
        self.assertEqual(self.invoice_calls, 1)

    def test_competing_workflow_refused_by_directory_lock(self):
        self.directory.mkdir()
        with lock(self.directory/'receive.lock'), self.assertRaises(BlockingIOError):
            self.invoke(lambda *a, **k: 0)
        self.assertEqual(self.invoice_calls, 0)

    def test_changed_request_refused(self):
        self.invoke(lambda *a, **k: 0)
        with patch('receive_workflow.Lab.rpc', side_effect=self.rpc), self.assertRaises(ValueError):
            receive(self.f.config, self.receiver, self.directory, 300000, threading.Event())


class RepaymentTests(unittest.TestCase):
    def setUp(self):
        self.f = test_market_quotes.MarketTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.quote()
        path, state = self.f.prepared()
        save(path, dict(state, phase='btc_released'))
        self.directory = self.f.f.directory
        self.receiver = ['receiver']
        self.rows = {}
        self.attempts = []
        self.sends = 0
        self.lose_reply = False
        self.preimage = 'cd'*32
        self.hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
        self.available = 345000000

    def rpc(self, cli, method, *args):
        if cli == self.receiver or cli == [*self.receiver, '-k']:
            if method == 'getinfo':
                return dict(network='xbt', id='receiver')
            if method == 'listpeerchannels':
                return {'channels': [dict(self.f.f.channel, peer_id='/xbt', spendable_msat=self.available)]}
            if method == 'listinvoices':
                return {'invoices': [dict(status='paid', payment_hash=self.f.f.decoded['payment_hash'],
                                          amount_received_msat=350000000)]}
            if method == 'decode':
                return dict(valid=True, currency='xbt', payee='/xbt', amount_msat=345000000,
                            payment_hash=self.hash, payment_secret='ee'*32,
                            min_final_cltv_expiry=18, created_at=int(time.time()), expiry=1200)
            if method == 'listsendpays':
                return {'payments': self.attempts}
            if method == 'sendpay':
                self.sends += 1
                self.attempts = [dict(payment_hash=self.hash, amount_msat=345000000, status='pending')]
                if self.lose_reply:
                    raise TimeoutError()
                return {}
            if method == 'waitsendpay':
                self.settle()
                return {'status': 'complete'}
        if cli == ['/xbt']:
            if method == 'listinvoices':
                return {'invoices': [self.rows[args[0]]] if args[0] in self.rows else []}
            if method == 'invoice':
                self.rows[args[1]] = dict(amount_msat=int(args[0][:-4]), status='unpaid',
                                          payment_hash=self.hash, bolt11='lnxbt-repayment')
                return self.rows[args[1]]
        return self.f.rpc(cli, method, *args)

    def settle(self):
        self.attempts[0].update(status='complete', payment_preimage=self.preimage)
        for row in self.rows.values():
            row.update(status='paid', amount_received_msat=345000000)

    def test_repay_caps_at_spendable_and_repeat_does_not_send(self):
        with patch('receive_workflow.Lab.rpc', side_effect=self.rpc), contextlib.redirect_stdout(io.StringIO()) as output:
            repay(self.directory, self.receiver)
            self.available = 0
            repay(self.directory, self.receiver)
        self.assertEqual(self.sends, 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['returned_xbt_sats'], 345000)
        self.assertNotIn(self.hash, output.getvalue())

    def test_lost_send_reply_reconciles_without_resend(self):
        self.lose_reply = True
        with patch('receive_workflow.Lab.rpc', side_effect=self.rpc), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                repay(self.directory, self.receiver)
            repay(self.directory, self.receiver)
            self.settle()
            repay(self.directory, self.receiver)
        self.assertEqual(self.sends, 1)

    def test_missing_attempt_never_resends(self):
        self.lose_reply = True
        with patch('receive_workflow.Lab.rpc', side_effect=self.rpc):
            with self.assertRaises(TimeoutError):
                repay(self.directory, self.receiver)
            self.attempts = []
            with self.assertRaises(ValueError):
                repay(self.directory, self.receiver)
        self.assertEqual(self.sends, 1)


if __name__ == '__main__':
    unittest.main()
