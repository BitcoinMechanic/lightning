"""Separate receiver setup and invoice handoff; no receiver RPC or live funds."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import remote_receiver
import swap_service
from swap_controller import run
import test_market_quotes


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.f = test_market_quotes.MarketTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.f.root
        self.source = self.root/'source.json'
        self.source.write_text(json.dumps(self.f.config))
        self.original = self.source.read_bytes()
        self.peer = '02' + 'ab'*32
        self.peer_file = self.root/'receiver.txt'
        self.peer_file.write_text(self.peer+'\n')
        self.target = self.root/'remote.json'
        self.f.f.channel['peer_id'] = self.peer
        self.f.f.decoded['payee'] = self.peer
        self.calls = []

    def rpc(self, cli, method, *args):
        self.assertIn(cli, (['/btc'], ['/xbt'], ['/xbt', '-k']))
        self.calls.append(method)
        return self.f.rpc(cli, method, *args)

    def bind(self):
        with patch('remote_receiver.RPC.call', side_effect=self.rpc):
            return remote_receiver.bind(self.source, self.peer_file, self.target)

    def test_binding_preserves_caps_and_source_and_uses_only_operator_reads(self):
        result = self.bind()
        self.assertFalse(result['receiver_rpc_required'])
        actual = json.loads(self.target.read_text())
        expected = dict(self.f.config, market=dict(self.f.config['market'], xbt_peer=self.peer))
        self.assertEqual(actual, expected)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(set(self.calls), {'getinfo', 'listfunds', 'listpeerchannels'})
        before = self.target.read_bytes()
        self.bind()
        self.assertEqual(before, self.target.read_bytes())

    def test_wrong_disconnected_pending_and_ambiguous_channel_refused(self):
        for key, value in [('peer_id', 'other'), ('state', 'ONCHAIN'),
                           ('peer_connected', False), ('htlcs', [{'id': 1}]),
                           ('spendable_msat', 0)]:
            old = dict(self.f.f.channel)
            self.f.f.channel[key] = value
            with self.assertRaises(ValueError):
                self.bind()
            self.assertFalse(self.target.exists())
            self.f.f.channel = old
        def duplicate(cli, method, *args):
            if cli == ['/xbt'] and method == 'listpeerchannels':
                return {'channels': [self.f.f.channel, dict(self.f.f.channel, short_channel_id='11x1x0')]}
            return self.rpc(cli, method, *args)
        with patch('remote_receiver.RPC.call', side_effect=duplicate), self.assertRaises(ValueError):
            remote_receiver.bind(self.source, self.peer_file, self.target)

    def test_different_existing_config_and_source_overwrite_refused(self):
        self.target.write_text('{}')
        with self.assertRaises(ValueError):
            self.bind()
        self.assertEqual(self.target.read_text(), '{}')
        with self.assertRaises(ValueError):
            remote_receiver.bind(self.source, self.peer_file, self.source)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_bad_peer_file_refused_before_rpc(self):
        for raw in (b'x', b'\xff', b'02'+b'a'*200, b'03'+b'z'*64):
            self.peer_file.write_bytes(raw)
            with patch('remote_receiver.RPC.call') as rpc, self.assertRaises(ValueError):
                remote_receiver.bind(self.source, self.peer_file, self.target)
            rpc.assert_not_called()

    def test_external_invoice_settles_with_only_operator_rpcs(self):
        self.bind()
        self.f.config = json.loads(self.target.read_text())
        invoice_file = self.root/'invoice.txt'
        invoice_file.write_text('lnxbt-remote-fixture\n')
        with patch('market_policy.fetch', side_effect=[self.f.ticker, self.f.book]), \
                patch('swap_service.RPC.call', side_effect=self.rpc):
            swap_service.create(self.f.config, swap_service.invoice_from_file(invoice_file),
                                None, self.f.f.directory)
            swap_service.publish(self.f.f.directory)
        path, state = self.f.prepared()
        self.assertEqual(state['route'][0]['id'], self.peer)
        with patch('swap_controller.RPC.call', side_effect=self.rpc), \
                patch('market_policy.fetch', side_effect=AssertionError('no recovery repricing')):
            self.assertEqual(run(path)['phase'], 'btc_released')
            self.assertEqual(run(path)['phase'], 'btc_released')
        self.assertEqual(self.f.sends, 1)
        self.assertNotIn('invoice', self.calls)
        self.assertNotIn('listinvoices', self.calls)

    def test_invoice_file_rejects_multiple_oversize_and_nonascii_without_leak(self):
        path = self.root/'invoice.txt'
        for raw in (b'', b'SECRET\nOTHER', b'x'*65537, b'SECRET\xff'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError) as error:
                swap_service.invoice_from_file(path)
            self.assertNotIn('SECRET', str(error.exception))

    def test_quote_market_cli_accepts_file(self):
        directory = self.root/'command'
        directory.mkdir()
        invoice = self.root/'invoice.txt'
        invoice.write_text('lnxbt-file-fixture\n')
        argv = ['swap_service.py', 'quote-market', '--directory', str(directory),
                '--config', str(self.source), '--xbt-invoice-file', str(invoice)]
        with patch('sys.argv', argv), patch('swap_service.create') as create, \
                patch('swap_service.publish', return_value={}), patch('swap_service.emit'), \
                patch('swap_service.signal.signal'):
            self.assertEqual(swap_service.main(), 0)
        self.assertEqual(create.call_args.args[1], 'lnxbt-file-fixture')


if __name__ == '__main__':
    unittest.main()
