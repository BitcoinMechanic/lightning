"""Offline oracle quote, immutable pricing, gate serialization and recovery."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import live_pilot as pilot
from market_policy import policy
from neoxa_oracle import PAIR
from swap_service import create, publish
from swap_controller import run, save
import test_live_pilot


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.f = test_live_pilot.PilotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.preimage = 'ab'*32
        self.f.decoded.update(amount_msat=350000000,
                             payment_hash=hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest())
        self.f.channel.update(spendable_msat=490000000, feerate={'perkw': 1250})
        self.incoming = dict(self.f.channel, short_channel_id='20x1x0', receivable_msat=90000000,
                             htlcs=[], feerate={'perkw': 829})
        self.config = dict(self.f.config, profile=pilot.PROFILE_MARKET, market=dict(
            btc_channel='20x1x0', xbt_channel='10x1x0', xbt_peer='receiver',
            max_btc_sats=3000, max_xbt_sats=400000, margin_bps=100))
        self.ticker = dict(success=True, pair=PAIR, ticker=dict(
            computedAt=time.time_ns()//1000000, bestBid='0.00449', bestAsk='0.0045'))
        self.book = dict(success=True, pair=PAIR, asks=[dict(price='0.0045', quantity='10')])
        self.registered = []
        self.attempts = []
        self.sends = 0
        self.phase = 'held'
        self.pending = False

    def rpc(self, cli, method, *args):
        if method == 'listpeerchannels' and cli == ['/btc']:
            return {'channels': [self.incoming]}
        if method == 'xbt-register':
            self.registered.append(json.loads(args[0]))
            return {'registered': True}
        if method == 'signinvoice':
            return {'bolt11': 'signed-market'}
        if method == 'decode' and cli == ['/btc']:
            terms = self.registered[-1]
            return dict(valid=True, currency='bc', payee='/btc', min_final_cltv_expiry=300,
                        payment_hash=terms['payment_hash'], payment_secret=terms['payment_secret'],
                        amount_msat=terms['btc_amount_msat'])
        if method == 'listsendpays':
            return {'payments': self.attempts}
        if method == 'xbt-spend-info':
            return dict(self.registered[-1], binding=['20x1x0', 7], cltv_expiry=400)
        if method == 'sendpay':
            self.sends += 1
            self.attempts = [dict(payment_hash=self.f.decoded['payment_hash'],
                                  amount_msat=350000000, status='pending')]
            return {}
        if method == 'waitsendpay':
            if self.pending:
                raise subprocess.TimeoutExpired('redacted', 1)
            self.complete()
            return {'status': 'complete'}
        if method == 'xbt-quote-status':
            return dict(payment_hash=self.f.decoded['payment_hash'], binding=['20x1x0', 7], phase=self.phase)
        if method == 'xbt-release':
            self.assertEqual(args, (self.preimage,))
            self.phase = 'resolved'
            return {'released': 1}
        return self.f.rpc(cli, method, *args)

    def complete(self):
        self.attempts[0].update(status='complete', payment_preimage=self.preimage)

    def quote(self):
        with patch('market_policy.fetch', side_effect=[self.ticker, self.book]), \
                patch('swap_service.Lab.rpc', side_effect=self.rpc):
            create(self.config, 'lnxbt-market', None, self.f.directory)
            return publish(self.f.directory)

    def prepared(self):
        data = json.loads((self.f.directory/'quote.json').read_text())
        state = dict(data['controller'], btc_binding=['20x1x0', 7])
        self.incoming['htlcs'] = [dict(id=7, direction='in', payment_hash=state['payment_hash'],
                                       state='RCVD_ADD_ACK_REVOCATION', amount_msat=state['btc_amount_msat'])]
        path = self.f.directory/'state.json'
        save(path, state)
        return path, state

    def test_price_frozen_and_success_reconciles_without_oracle(self):
        result = self.quote()
        self.assertEqual(result['btc_sats'], 1591)
        path, state = self.prepared()
        with patch('swap_controller.Lab.rpc', side_effect=self.rpc), \
                patch('market_policy.fetch', side_effect=AssertionError('must not reprice')):
            self.assertEqual(run(path)['phase'], 'btc_released')
            self.assertEqual(run(path)['phase'], 'btc_released')
        self.assertEqual(self.sends, 1)
        self.assertEqual(json.loads(path.read_text())['oracle'], state['oracle'])

    def test_pending_recovery_never_reprices_or_resends(self):
        self.quote()
        path, state = self.prepared()
        self.pending = True
        with patch('swap_controller.Lab.rpc', side_effect=self.rpc), \
                patch('market_policy.fetch', side_effect=AssertionError('must not reprice')):
            with self.assertRaises(subprocess.TimeoutExpired):
                run(path)
            before = path.read_bytes()
            run(path)
            self.assertEqual(before, path.read_bytes())
            self.complete()
            self.assertEqual(run(path)['phase'], 'btc_released')
        self.assertEqual(self.sends, 1)

    def test_price_and_route_tampering_refused(self):
        self.quote()
        path, state = self.prepared()
        for key, value in (('btc_amount_msat', 1), ('oracle_digest', '00'*32),
                           ('btc_channel', 'wrong')):
            save(path, dict(state, **{key: value}))
            with patch('swap_controller.Lab.rpc', side_effect=self.rpc), self.assertRaises(RuntimeError):
                run(path)
        self.assertEqual(self.sends, 0)

    def test_manual_price_caps_and_wrong_receiver_refused(self):
        with self.assertRaises(ValueError):
            create(self.config, 'lnxbt-market', 1591, self.f.directory)
        self.config['market']['max_xbt_sats'] = 500001
        with self.assertRaises(ValueError):
            policy(self.config)
        self.config['market']['max_xbt_sats'] = 400000
        self.config['market']['xbt_peer'] = 'wrong'
        with patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(ValueError):
            create(self.config, 'lnxbt-market', None, self.f.directory)
        self.assertFalse(self.f.directory.exists())

    def test_expensive_btc_and_stale_publication_refused(self):
        self.config['market']['max_btc_sats'] = 1000
        with patch('market_policy.fetch', side_effect=[self.ticker, self.book]), \
                patch('swap_service.Lab.rpc', side_effect=self.rpc), self.assertRaises(ValueError):
            create(self.config, 'lnxbt-market', None, self.f.directory)
        self.assertFalse(self.f.directory.exists())
        self.config['market']['max_btc_sats'] = 3000
        with patch('market_policy.fetch', side_effect=[self.ticker, self.book]), \
                patch('swap_service.Lab.rpc', side_effect=self.rpc):
            create(self.config, 'lnxbt-market', None, self.f.directory)
            with patch('market_policy.time.time_ns', return_value=(self.ticker['ticker']['computedAt']+30001)*1000000), \
                    self.assertRaises(RuntimeError):
                publish(self.f.directory)
        self.assertEqual(self.registered, [])

    def test_setup_pins_replacement_channel_without_overwriting(self):
        from market_setup import setup
        self.quote()
        path, state = self.prepared()
        old = json.loads((self.f.directory/'quote.json').read_text())
        old['config']['profile'] = pilot.PROFILE_V2
        (self.f.directory/'quote.json').write_text(json.dumps(old))
        state['phase'] = 'btc_released'
        save(path, state)
        self.phase = 'resolved'
        self.incoming['htlcs'] = []
        target = self.f.root/'market-config.json'
        with patch('market_setup.Lab.rpc', side_effect=self.rpc):
            self.assertTrue(setup(self.f.directory, target, 3000, 400000, 100)['config_ready'])
            before = target.read_bytes()
            setup(self.f.directory, target, 3000, 400000, 100)
            self.assertEqual(target.read_bytes(), before)
            with self.assertRaises(ValueError):
                setup(self.f.directory, target, 3000, 300000, 100)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_text())['market']['xbt_channel'], '10x1x0')

    def test_gate_serializes_distinct_quotes_and_retains_history(self):
        self.quote()
        terms = self.registered[-1]
        plugin = self.f.root/'market_gate.py'
        plugin.write_text(Path(__file__).with_name('quote_plugin.py').read_text())
        path = plugin.with_suffix('.quotes.json')
        old = {'aa'*32: {'phase': 'resolved', 'terms': {'pilot': 'live-pilot-v2'}}}
        path.write_text(json.dumps(old))
        def execute(items):
            requests = [dict(id=1, method='init', params={
                'configuration': {'network': 'bitcoin'},
                'options': {'xbt-live-pilot': pilot.PROFILE_MARKET}})]
            requests += [dict(id=i, method='xbt-register', params=[t]) for i,t in enumerate(items,2)]
            result = subprocess.run([sys.executable, str(plugin)], input='\n\n'.join(map(json.dumps,requests))+'\n\n',
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            return {r['id']:r for line in result.stdout.splitlines() if line.strip()
                    for r in [json.loads(line)] if 'id' in r}
        replies = execute([dict(terms, btc_amount_msat=10000001), terms, terms,
                           dict(terms, payment_hash='bb'*32), dict(terms, controller_id='cc'*32)])
        self.assertIn('error', replies[2])
        self.assertEqual(replies[3]['result'], {'registered': True})
        self.assertEqual(replies[4]['result'], {'registered': True})
        self.assertIn('error', replies[5])
        self.assertIn('error', replies[6])
        self.assertIn('error', execute([dict(terms,payment_hash='bb'*32)])[2])
        stored = json.loads(path.read_text())
        self.assertEqual(stored['aa'*32], old['aa'*32])
        stored[terms['payment_hash']]['terms']['expires_at'] = 1
        stored[terms['payment_hash']]['phase'] = 'held'
        path.write_text(json.dumps(stored))
        self.assertIn('error', execute([dict(terms,payment_hash='bb'*32)])[2])
        stored[terms['payment_hash']]['phase'] = 'resolved'
        path.write_text(json.dumps(stored))
        self.assertEqual(execute([dict(terms,payment_hash='bb'*32)])[2]['result'], {'registered': True})


if __name__ == '__main__':
    unittest.main()
