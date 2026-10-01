"""Service setup persistence and recovery boundaries; no live nodes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from swap_controller import save
from swap_service import create, publish, serve, status


class ServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.directory = self.root / 'swap'
        self.config = {'btc_cli': ['/btc'], 'xbt_cli': ['/xbt']}
        self.decoded = {'valid': True, 'type': 'bolt11 invoice', 'currency': 'xbtrt',
                        'amount_msat': 200000000, 'payment_hash': 'ab' * 32,
                        'payment_secret': 'cd' * 32, 'created_at': int(time.time()),
                        'expiry': 3600, 'min_final_cltv_expiry': 18, 'payee': 'receiver'}
        self.channels = [{'peer_id': 'receiver', 'state': 'CHANNELD_NORMAL',
                          'short_channel_id': '1x1x0', 'spendable_msat': 300000000}]
        self.calls = []

    def rpc(self, cli, method, *args):
        self.calls.append(method)
        if method == 'getinfo':
            return {'id': cli[0], 'network': 'regtest' if cli == ['/btc'] else 'xbt-regtest'}
        if method == 'decode' and cli == ['/xbt']:
            return self.decoded
        if method == 'listpeerchannels':
            return {'channels': self.channels}
        if method == 'listsendpays':
            return {'payments': []}
        if method == 'xbt-register':
            # The complete setup must already be durable before registration.
            self.assertEqual(json.loads((self.directory / 'quote.json').read_text())['terms'], json.loads(args[0]))
            return {'registered': True}
        if method == 'signinvoice':
            self.assertTrue(args[0].startswith('lnbcrt1230000000p1'))
            return {'bolt11': 'signed-btc'}
        if method == 'decode':
            terms = json.loads((self.directory / 'quote.json').read_text())['terms']
            return dict(valid=True, currency='bcrt', payee='/btc', min_final_cltv_expiry=120,
                        payment_hash=terms['payment_hash'], payment_secret=terms['payment_secret'],
                        amount_msat=terms['btc_amount_msat'])
        raise AssertionError(method)

    def setup_quote(self):
        with patch('swap_service.RPC.call', side_effect=self.rpc):
            create(self.config, 'lnxbtrt-fixture', 123000, self.directory)
            return publish(self.directory)

    def test_quote_persisted_before_registration_and_publication_reusable(self):
        quote = self.setup_quote()
        self.assertEqual(quote['btc_sats'], 123000)
        before = (self.directory / 'quote.json').read_bytes()
        with patch('swap_service.RPC.call', side_effect=self.rpc):
            self.assertEqual(publish(self.directory), quote)
        self.assertEqual((self.directory / 'quote.json').read_bytes(), before)
        self.assertEqual(self.calls.count('signinvoice'), 1)
        self.assertEqual((self.directory / 'quote.json').stat().st_mode & 0o777, 0o600)

    def test_bad_invoice_and_unusable_route_do_not_create_quote(self):
        for field, bad in (('currency', 'bc'), ('valid', False), ('amount_msat', None),
                           ('min_final_cltv_expiry', 41), ('expiry', 1)):
            with self.subTest(field=field):
                old = self.decoded[field]
                self.decoded[field] = bad
                with patch('swap_service.RPC.call', side_effect=self.rpc), self.assertRaises(ValueError):
                    create(self.config, 'lnxbtrt-fixture', 123000, self.directory)
                self.decoded[field] = old
                self.assertFalse(self.directory.exists())
        self.channels = []
        with patch('swap_service.RPC.call', side_effect=self.rpc), self.assertRaises(ValueError):
            create(self.config, 'lnxbtrt-fixture', 123000, self.directory)

    def test_lost_submission_reply_enters_recovery_not_another_start(self):
        self.setup_quote()
        path = self.directory / 'state.json'
        save(path, {'phase': 'prepared'})
        def interrupted(_):
            save(path, {'phase': 'outgoing_started'})
            raise subprocess.TimeoutExpired(['sendpay'], 20)
        with patch('swap_service.RPC.call', side_effect=self.rpc), \
                patch('swap_service.reconcile', side_effect=interrupted) as start, \
                patch('swap_service.watch', return_value=0) as watcher:
            self.assertEqual(serve(self.directory, threading.Event()), 0)
            self.assertEqual(serve(self.directory, threading.Event()), 0)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(watcher.call_count, 2)

    def test_status_omits_secrets(self):
        self.setup_quote()
        save(self.directory / 'state.json', {'phase': 'btc_released', 'preimage': 'ef' * 32})
        report = status(self.directory)
        self.assertEqual(report['phase'], 'btc_released')
        self.assertNotIn('ef' * 32, json.dumps(report))
        self.assertNotIn('payment_secret', json.dumps(report))

    def test_registration_retry_preserves_held_binding(self):
        self.setup_quote()
        terms = json.loads((self.directory / 'quote.json').read_text())['terms']
        plugin = self.root / 'quote_plugin.py'
        plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        path = plugin.with_suffix('.quotes.json')
        entry = {'terms': terms, 'phase': 'held', 'binding': ['1x1x0', 2]}
        save(path, {terms['payment_hash']: entry})
        before = path.read_bytes()
        requests = [dict(id=1, method='init', params={'configuration': {'network': 'regtest'}}),
                    dict(id=2, method='xbt-register', params=[terms]),
                    dict(id=3, method='xbt-register', params=[dict(terms, btc_amount_msat=1)])]
        result = subprocess.run([sys.executable, str(plugin)],
                                input='\n\n'.join(map(json.dumps, requests)) + '\n\n',
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(replies[1]['result'], {'registered': True})
        self.assertIn('error', replies[2])
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
