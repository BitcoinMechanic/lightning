"""Market feasibility boundaries, with no network calls or spending."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_check import capacity, check
from neoxa_oracle import estimate, PAIR


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.btc = dict(state='CHANNELD_NORMAL', peer_connected=True, htlcs=[],
                        feerate={'perkw': 829}, dust_limit_msat=546000,
                        receivable_msat=90000000, short_channel_id='1x1x1')
        self.xbt = dict(self.btc, feerate={'perkw': 1250}, spendable_msat=96000000,
                        short_channel_id='2x2x2', peer_id='receiver')
        self.ticker = dict(success=True, pair=PAIR, ticker=dict(
            computedAt=1000000, bestBid='0.0044', bestAsk='0.004428'))
        self.book = dict(success=True, pair=PAIR, asks=[dict(price='0.004428', quantity='10')])

    def run_check(self):
        return capacity(self.btc, self.xbt, self.ticker, self.book,
                        now_ms=1000001, margin_bps=100)

    def price(self, sats):
        return estimate(self.ticker, self.book, sats, now_ms=1000001, margin_bps=100)['btc_sats']

    def test_current_channel_cannot_cover_btc_minimum(self):
        result = self.run_check()
        self.assertFalse(result['feasible'])
        self.assertEqual(result['btc_minimum_sats'], 1130)
        self.assertEqual(result['xbt_minimum_sats'], 1426)
        self.assertEqual(result['btc_sats_at_full_xbt_balance'], 430)

    def test_exact_lower_boundary_and_receivable_upper_boundary(self):
        self.xbt['spendable_msat'] = 1000000000
        self.btc['receivable_msat'] = 2000000
        result = self.run_check()
        self.assertTrue(result['feasible'])
        lo, hi = result['minimum_xbt_sats'], result['maximum_xbt_sats']
        self.assertLess(self.price(lo-1), 1130)
        self.assertGreaterEqual(self.price(lo), 1130)
        self.assertLessEqual(self.price(hi), 2000)
        self.assertGreater(self.price(hi+1), 2000)

    def test_disconnected_pending_or_closed_refused(self):
        for override in ({'peer_connected': False}, {'htlcs': [{}]}, {'state': 'ONCHAIN'}):
            old = self.btc
            self.btc = dict(old, **override)
            with self.assertRaises(ValueError):
                self.run_check()
            self.btc = old

    def test_stale_market_refused(self):
        self.ticker['ticker']['computedAt'] = 1
        with self.assertRaises(ValueError):
            self.run_check()

    def test_low_balances_and_invalid_fee(self):
        self.btc['receivable_msat'] = 1
        self.assertFalse(self.run_check()['feasible'])
        self.xbt['feerate']['perkw'] = 0
        with self.assertRaises(ValueError):
            self.run_check()

    def test_historical_records_unchanged_and_only_read_rpcs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            quote = dict(config=dict(profile='live-pilot-v2', btc_cli=['btc'], xbt_cli=['xbt']),
                         node_ids=['btc', 'xbt'], terms={'btc_channel': '1x1x1'},
                         controller={'route': [{'channel': '2x2x2', 'id': 'receiver'}]})
            (directory/'quote.json').write_text(json.dumps(quote))
            (directory/'state.json').write_text('{"phase":"btc_released"}')
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            def rpc(cli, method):
                if method == 'getinfo':
                    return dict(network='bitcoin' if cli == ['btc'] else 'xbt', id=cli[0])
                if method == 'listfunds':
                    return {'outputs': [dict(status='confirmed', reserved=False, amount_msat=50000000)]}
                if method == 'listpeerchannels':
                    return {'channels': [self.btc if cli == ['btc'] else self.xbt]}
                self.fail('unexpected RPC: '+method)
            with patch('market_check.Lab.rpc', side_effect=rpc), \
                    patch('market_check.fetch', side_effect=[self.ticker, self.book]), \
                    patch('market_check.time.time_ns', return_value=1000001000000):
                self.assertFalse(check(directory, 100)['feasible'])
            self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})


if __name__ == '__main__':
    unittest.main()
