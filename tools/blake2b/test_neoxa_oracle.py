"""Deterministic oracle boundaries; no network or payment calls."""
import copy
from decimal import Decimal
import unittest
from neoxa_oracle import estimate, number, PAIR


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.ticker = dict(success=True, pair=PAIR, ticker=dict(
            computedAt=1000000, bestBid='0.004', bestAsk='0.00401'))
        self.book = dict(success=True, pair=PAIR, asks=[
            dict(price='0.00401', quantity='999', isAmm=True),
            dict(price='0.00402', quantity='0.001'),
            dict(price='0.00403', quantity='1')])

    def quote(self, **kwargs):
        return estimate(self.ticker, self.book, 200000, now_ms=1000001, **kwargs)

    def test_depth_margin_and_round_up(self):
        result = self.quote(margin_bps=100)
        self.assertEqual(result['btc_sats'], 814)  # 805 sats + 1%, rounded upward.
        self.assertEqual(result['average_btc_per_xbt'], '0.004025')
        self.assertEqual(len(result['fills']), 2)

    def test_stale_future_and_boundary(self):
        self.ticker['ticker']['computedAt'] = 970001
        self.quote()
        for stamp in (970000, 1000002, '1000000', True):
            self.ticker['ticker']['computedAt'] = stamp
            with self.assertRaises(ValueError):
                self.quote()

    def test_amm_is_not_counted_as_limit_liquidity(self):
        self.book['asks'] = self.book['asks'][:1]
        with self.assertRaises(ValueError):
            self.quote()

    def test_insufficient_depth(self):
        self.book['asks'] = self.book['asks'][1:2]
        with self.assertRaises(ValueError):
            self.quote()

    def test_market_and_spread_checks(self):
        for key, value in (('pair', 'BTC_BTCB2'), ('success', False)):
            bad = copy.deepcopy(self.ticker)
            bad[key] = value
            with self.assertRaises(ValueError):
                estimate(bad, self.book, 200000, now_ms=1000001)
        for ask in ('0.003', '0.005'):
            self.ticker['ticker']['bestAsk'] = ask
            with self.assertRaises(ValueError):
                self.quote()

    def test_limits(self):
        for settings in (dict(max_slippage_bps=1), dict(min_price='0.005'),
                         dict(max_price='0.004'), dict(margin_bps=-1)):
            with self.assertRaises(ValueError):
                self.quote(**settings)

    def test_amm_reference_gap_separate_from_depth(self):
        self.ticker['ticker'].update(bestBid='0.00399', bestAsk='0.004')
        self.book['asks'] = [dict(price='0.004', quantity='999', isAmm=True),
                             dict(price='0.00406', quantity='1')]
        result = self.quote()
        self.assertEqual(result['btc_sats'], 812)
        self.assertEqual(Decimal(result['depth_slippage_bps']), 0)
        self.assertEqual(Decimal(result['reference_gap_bps']), 150)
        self.assertEqual(result['best_limit_ask_btc_per_xbt'], '0.00406')
        self.assertEqual(result['ticker_best_ask_btc_per_xbt'], '0.004')
        self.assertEqual(result['policy']['max_reference_gap_bps'], 200)
        self.assertEqual(result['policy']['max_slippage_bps'], 100)
        self.assertEqual(len(result['fills']), 1)
        with self.assertRaisesRegex(ValueError, 'reference gap limit'):
            self.quote(max_reference_gap_bps=100)

    def test_reference_boundary_independent_of_slippage_cap(self):
        self.ticker['ticker'].update(bestBid='0.0039', bestAsk='0.004')
        for price in ('0.00408', '0.00392'):
            self.book['asks'] = [dict(price=price, quantity='1')]
            self.assertEqual(Decimal(self.quote()['reference_gap_bps']), 200)
        for price in ('0.00408000001', '0.00391999999'):
            self.book['asks'] = [dict(price=price, quantity='1')]
            with self.assertRaisesRegex(ValueError, 'reference gap limit'):
                self.quote(max_slippage_bps=10000)

    def test_depth_slippage_boundary_independent_of_reference_cap(self):
        self.ticker['ticker'].update(bestBid='0.00399', bestAsk='0.004')
        self.book['asks'] = [dict(price='0.004', quantity='0.001'),
                             dict(price='0.00408', quantity='1')]
        result = self.quote()
        self.assertEqual(Decimal(result['depth_slippage_bps']), 100)
        self.assertEqual(Decimal(result['reference_gap_bps']), 0)
        self.book['asks'][1]['price'] = '0.00408000001'
        with self.assertRaisesRegex(ValueError, 'slippage limit'):
            self.quote(max_reference_gap_bps=10000)

    def test_reference_policy_rejects_invalid_values(self):
        for value in (-1, 10001, True, '200'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.quote(max_reference_gap_bps=value)

    def test_current_snapshot_fill_and_rounded_margin(self):
        self.ticker['ticker'].update(bestBid='0.0043925', bestAsk='0.00446332')
        self.book['asks'] = [dict(price='0.00446332', quantity='0.16865267', isAmm=True),
                             dict(price='0.00448489', quantity='0.27692188')]
        result = estimate(self.ticker, self.book, 327827, now_ms=1000001, margin_bps=100)
        self.assertEqual(result['btc_sats'], 1485)
        self.assertEqual(Decimal(result['depth_slippage_bps']), 0)
        self.assertLess(Decimal(result['reference_gap_bps']), 100)

    def test_nonfinite_and_invalid_numbers(self):
        for value in ('NaN', 'Infinity', '-1', '0', True, None, '1e999', '1e-999'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                number(value)
        self.assertEqual(number(Decimal('0.00402')), Decimal('0.00402'))


if __name__ == '__main__':
    unittest.main()
