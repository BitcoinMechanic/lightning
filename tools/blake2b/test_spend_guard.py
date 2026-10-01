"""Offline boundaries and fail-closed behavior of the pre-spend check."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swap_controller import check_spend, run, save


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.state = {'phase': 'prepared', 'quote_gate': True, 'btc_cli': ['btc'],
                      'xbt_cli': ['xbt'], 'xbt_invoice': 'lnxbtrt-test', 'payment_secret': '22' * 32,
                      'route': [{'id': 'receiver', 'amount_msat': 200000000, 'delay': 40}],
                      'payment_hash': '11' * 32, 'btc_binding': ['104x1x0', 0],
                      'xbt_amount_msat': 200000000}
        self.info = {'payment_hash': '11' * 32, 'binding': ['104x1x0', 0],
                     'xbt_invoice': 'lnxbtrt-test',
                     'xbt_amount_msat': 200000000, 'cltv_expiry': 229,
                     'min_cltv_delta': 100, 'max_cltv_delta': 2000, 'expires_at': 2000}
        self.decoded = {'valid': True, 'type': 'bolt11 invoice', 'currency': 'xbtrt',
                        'payment_hash': '11' * 32, 'payment_secret': '22' * 32,
                        'amount_msat': 200000000, 'created_at': 900, 'expiry': 1000,
                        'payee': 'receiver', 'min_final_cltv_expiry': 18}

    def test_current_height_boundary(self):
        for height, expected in ((109, None), (129, None), (130, 'insufficient_btc_cltv')):
            with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': height}, self.decoded]), \
                    patch('swap_controller.time.time', return_value=1000):
                self.assertEqual(check_spend(self.state), expected)

    def test_expired_quote(self):
        with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': 109}]), \
                patch('swap_controller.time.time', return_value=2000):
            self.assertEqual(check_spend(self.state), 'quote_expired')

    def test_identity_and_amount_mismatch(self):
        for override in ({'binding': ['104x1x0', 1]}, {'payment_hash': '22' * 32},
                         {'xbt_amount_msat': 1}):
            with patch('swap_controller.RPC.call', return_value=dict(self.info, **override)):
                with self.assertRaises(RuntimeError):
                    check_spend(self.state)

    def test_refusal_preserves_state_and_never_calls_xbt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            save(path, self.state)
            before = path.read_bytes()
            with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': 130}]) as rpc, \
                    patch('swap_controller.time.time', return_value=1000):
                self.assertEqual(run(path), {'phase': 'prepared', 'outcome': 'refused',
                                             'reason': 'insufficient_btc_cltv'})
                self.assertEqual([c.args for c in rpc.call_args_list],
                                 [(['btc'], 'xbt-spend-info', '11' * 32), (['btc'], 'getinfo')])
            self.assertEqual(path.read_bytes(), before)
            with patch('swap_controller.RPC.call', side_effect=RuntimeError('RPC unavailable')):
                with self.assertRaises(RuntimeError):
                    run(path)
            self.assertEqual(path.read_bytes(), before)

    def test_substituted_invoice_refused_without_decode(self):
        with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': 109}]) as rpc, \
                patch('swap_controller.time.time', return_value=1000):
            self.assertEqual(check_spend(dict(self.state, xbt_invoice='lnxbtrt-other')),
                             'quoted_invoice_mismatch')
            self.assertEqual(rpc.call_count, 2)

    def test_signed_fields_and_expiry(self):
        for override in ({'valid': False}, {'currency': 'bcrt'}, {'payment_hash': '33' * 32},
                         {'payment_secret': '33' * 32}, {'amount_msat': 1}, {'expiry': 100}):
            with self.subTest(override=override):
                with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': 109},
                           dict(self.decoded, **override)]), patch('swap_controller.time.time', return_value=1000):
                    self.assertIn(check_spend(self.state), ('xbt_invoice_fields_mismatch', 'xbt_invoice_expired'))

    def test_direct_route_must_match_invoice(self):
        for route in ([], [{'id': 'other', 'amount_msat': 200000000, 'delay': 40}],
                      [{'id': 'receiver', 'amount_msat': 1, 'delay': 40}],
                      [{'id': 'receiver', 'amount_msat': 200000000, 'delay': 17}]):
            with patch('swap_controller.RPC.call', side_effect=[self.info, {'blockheight': 109}, self.decoded]), \
                    patch('swap_controller.time.time', return_value=1000):
                self.assertEqual(check_spend(dict(self.state, route=route)), 'xbt_route_mismatch')


if __name__ == '__main__':
    unittest.main()
