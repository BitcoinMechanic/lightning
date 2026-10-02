"""Exact reverse bid pricing, whole-sat rounding and read-only CLI boundaries."""
import copy
from fractions import Fraction
import io
import json
import unittest
from unittest.mock import patch

import reverse_oracle as oracle


class ReverseOracleTests(unittest.TestCase):
    def setUp(self):
        self.ticker = dict(success=True, pair=oracle.PAIR,
                           ticker=dict(computedAt=1000000, bestBid='0.004', bestAsk='0.00401'))
        self.book = dict(success=True, pair=oracle.PAIR, bids=[
            dict(price='0.004', quantity='10')])

    def quote(self, btc_sats=1000, **kwargs):
        options = dict(max_routing_fee_sats=10, now_ms=1000001, margin_bps=100)
        options.update(kwargs)
        return oracle.estimate(self.ticker, self.book, btc_sats, **options)

    def test_bid_direction_fee_budget_and_margin(self):
        result = self.quote()
        self.assertEqual(result['xbt_sats'], 255025)  # (1000+10)*1.01 / .004
        self.assertEqual(result['btc_budget_sats'], 1010)
        self.assertEqual(result['target_bid_proceeds_btc_sats'], '1020.1')
        self.assertEqual(result['estimated_bid_proceeds_btc_sats'], '1020.1')
        self.assertEqual(result['mode'], 'limit-order-bids')
        self.assertTrue(result['read_only'])
        self.assertTrue(result['routing_fee_allowance_included'])
        self.assertFalse(result['route_checked'])
        self.assertFalse(result['channel_capacity_checked'])
        self.assertFalse(result['exchange_fees_included'])

    def test_multilevel_depth_sorted_best_bid_first(self):
        self.book['bids'] = [dict(price='0.00399', quantity='1'),
                             dict(price='0.004', quantity='0.001')]
        result = self.quote()
        self.assertEqual(result['fills'][0]['xbt_sats'], 100000)
        self.assertEqual(len(result['fills']), 2)
        target = Fraction('1020.1')
        proceeds = sum(Fraction(f['price_btc_per_xbt'])*f['xbt_sats'] for f in result['fills'])
        self.assertGreaterEqual(proceeds, target)
        self.assertLess(proceeds-Fraction('0.00399'), target)
        self.assertEqual(sum(f['xbt_sats'] for f in result['fills']), result['xbt_sats'])

    def test_exact_rounding_and_one_sat_less_cannot_cover(self):
        self.ticker['ticker'].update(bestBid='0.003', bestAsk='0.003')
        self.book['bids'][0]['price'] = '0.003'
        result = self.quote(1, max_routing_fee_sats=0, margin_bps=0)
        self.assertEqual(result['xbt_sats'], 334)
        self.assertEqual(result['estimated_bid_proceeds_btc_sats'], '1.002')
        result = self.quote(3, max_routing_fee_sats=0, margin_bps=0)
        self.assertEqual(result['xbt_sats'], 1000)

    def test_depth_rounds_down_and_insufficient_depth_refused(self):
        self.book['bids'][0]['quantity'] = '0.00255024999'
        with self.assertRaises(ValueError):
            self.quote()
        self.book['bids'][0]['quantity'] = '0.00255025'
        self.assertEqual(self.quote()['xbt_sats'], 255025)
        self.book['bids'][0]['quantity'] = '0.000000009'
        with self.assertRaises(ValueError):
            self.quote()

    def test_amm_samples_are_not_executable_depth(self):
        self.book['bids'][0]['isAmm'] = True
        with self.assertRaises(ValueError):
            self.quote()
        self.book['bids'].append(dict(price='0.004', quantity='1'))
        self.assertEqual(self.quote()['xbt_sats'], 255025)
        self.book['bids'][-1]['isAmm'] = 'false'
        with self.assertRaises(ValueError):
            self.quote()

    def test_timestamp_exact_boundary_future_and_stale(self):
        self.quote(now_ms=1030000)
        for value in (1030001, 999999, True, '1000001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.quote(now_ms=value)
        self.ticker['ticker']['computedAt'] = True
        with self.assertRaises(ValueError):
            self.quote()

    def test_spread_and_bid_slippage_boundaries(self):
        self.ticker['ticker']['bestAsk'] = '0.0042'
        self.quote()  # Exactly 500bps spread.
        self.ticker['ticker']['bestAsk'] = '0.0042000001'
        with self.assertRaises(ValueError):
            self.quote()
        self.ticker['ticker']['bestAsk'] = '0.00401'
        self.book['bids'][0]['price'] = '0.00392'
        result = self.quote()  # Exactly 200bps ticker-to-limit gap, no depth slippage.
        self.assertEqual(result['reference_gap_bps'], '200')
        self.assertEqual(result['depth_slippage_bps'], '0')
        self.book['bids'][0]['price'] = '0.0039199999'
        with self.assertRaisesRegex(ValueError, 'reference gap limit'):
            self.quote(max_slippage_bps=10000)

    def test_observed_amm_best_bid_is_not_limit_depth_reference(self):
        self.ticker['ticker'].update(bestBid='0.00442043', bestAsk='0.00449177')
        self.book['bids'] = [
            dict(price='0.00442043', quantity='0.18580521', isAmm=True),
            dict(price='0.00439821', quantity='0.17215822', isAmm=True),
            dict(price='0.0043639', quantity='0.7681374600000002')]
        result = self.quote(1500, max_routing_fee_sats=30)
        self.assertEqual(result['xbt_sats'], 354110)
        self.assertEqual(len(result['fills']), 1)
        self.assertEqual(result['best_limit_bid_btc_per_xbt'], '0.0043639')
        self.assertEqual(result['ticker_best_bid_btc_per_xbt'], '0.00442043')
        self.assertEqual(result['depth_slippage_bps'], '0')
        self.assertGreater(Fraction(result['reference_gap_bps']), 100)
        self.assertLess(Fraction(result['reference_gap_bps']), 200)
        self.assertEqual(result['policy']['max_reference_gap_bps'], 200)
        self.assertEqual(result['policy']['max_slippage_bps'], 100)
        with self.assertRaisesRegex(ValueError, 'reference gap limit'):
            self.quote(1500, max_routing_fee_sats=30, max_reference_gap_bps=100)

    def test_depth_slippage_boundary_independent_of_reference_gap(self):
        self.book['bids'] = [dict(price='0.004', quantity='0.0005'),
                             dict(price='0.00392', quantity='1')]
        result = self.quote(396, max_routing_fee_sats=0, margin_bps=0)
        self.assertEqual(result['xbt_sats'], 100000)
        self.assertEqual(result['depth_slippage_bps'], '100')
        self.assertEqual(result['reference_gap_bps'], '0')
        self.book['bids'][1]['price'] = '0.003919999'
        with self.assertRaisesRegex(ValueError, 'slippage limit'):
            self.quote(396, max_routing_fee_sats=0, margin_bps=0,
                       max_reference_gap_bps=10000)

    def test_reference_gap_bounds_both_directions(self):
        self.ticker['ticker']['bestAsk'] = '0.0042'
        self.book['bids'][0]['price'] = '0.00408'
        self.assertEqual(self.quote()['reference_gap_bps'], '200')
        self.book['bids'][0]['price'] = '0.0040800001'
        with self.assertRaisesRegex(ValueError, 'reference gap limit'):
            self.quote()


    def test_market_identity_crossed_and_inconsistent_book(self):
        for data in (self.ticker, self.book):
            for key, value in (('success', False), ('pair', 'BTC_BTCB2')):
                old = data[key]
                data[key] = value
                with self.assertRaises(ValueError):
                    self.quote()
                data[key] = old
        self.book['bids'][0]['price'] = '0.00402'
        with self.assertRaises(ValueError):
            self.quote()
        self.book['bids'][0]['price'] = '0.004'
        self.ticker['ticker']['bestAsk'] = '0.00399'
        with self.assertRaises(ValueError):
            self.quote()

    def test_invalid_amounts_policies_and_prices(self):
        for value in (0, -1, True, '1000', oracle.SUPPLY_SATS+1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.quote(value)
        for options in (dict(max_routing_fee_sats=-1), dict(max_routing_fee_sats=True),
                        dict(max_routing_fee_sats=oracle.SUPPLY_SATS), dict(margin_bps=-1),
                        dict(margin_bps=10001), dict(max_age_seconds=0),
                        dict(max_reference_gap_bps=-1), dict(max_reference_gap_bps=True),
                        dict(max_reference_gap_bps=10001), dict(max_reference_gap_bps='200'),
                        dict(min_price='0.005'), dict(max_price='0.003'),
                        dict(min_price='0.005', max_price='0.004')):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.quote(**options)
        for value in ('NaN', 'Infinity', '0', '-1', True):
            self.book['bids'][0]['price'] = value
            with self.assertRaises(ValueError):
                self.quote()

    def test_larger_btc_fee_or_margin_never_reduces_charge(self):
        base = self.quote()['xbt_sats']
        for options in (dict(max_routing_fee_sats=11), dict(margin_bps=101)):
            self.assertGreaterEqual(self.quote(**options)['xbt_sats'], base)
        self.assertGreater(self.quote(1001)['xbt_sats'], base)

    def test_input_snapshots_unchanged(self):
        before = copy.deepcopy((self.ticker, self.book))
        self.quote()
        self.assertEqual(before, (self.ticker, self.book))

    def test_cli_fetches_two_public_snapshots_only_and_fails_without_fallback(self):
        args = ['reverse_oracle.py', '--btc-sats', '1000', '--max-routing-fee-sats', '10', '--margin-bps', '100']
        with patch('sys.argv', args), patch('reverse_oracle.fetch', side_effect=[self.ticker, self.book]) as fetch, \
                patch('reverse_oracle.time.time_ns', return_value=1000001000000), \
                patch('reverse_oracle.time.monotonic', side_effect=[0, 1]), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(oracle.main(), 0)
            self.assertEqual(json.loads(out.getvalue())['xbt_sats'], 255025)
            self.assertEqual([c.args for c in fetch.call_args_list], [('ticker',), ('orderbook',)])
        for fetch_failure in (False, True):
            with patch('sys.argv', args), \
                    patch('reverse_oracle.fetch', side_effect=RuntimeError('PRIVATE') if fetch_failure else [self.ticker, self.book]), \
                    patch('reverse_oracle.time.monotonic', side_effect=[0, 16]), \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(oracle.main(), 1)
                self.assertEqual(json.loads(out.getvalue())['event'], 'oracle_unavailable')
                self.assertNotIn('PRIVATE', out.getvalue())


if __name__ == '__main__':
    unittest.main()
