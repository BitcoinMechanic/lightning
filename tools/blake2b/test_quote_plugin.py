"""Offline quote-gate regression tests; no daemons or third-party packages."""
import unittest

from quote_plugin import validate, validation_error


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.quote = {'payment_hash': '11' * 32, 'payment_secret': '22' * 32,
                      'btc_amount_msat': 100000000, 'expires_at': 2000,
                      'min_cltv_delta': 100, 'max_cltv_delta': 2000}
        self.htlc = {'payment_hash': '11' * 32, 'amount_msat': 100000000,
                     'cltv_expiry': 229, 'cltv_expiry_relative': 120}
        self.onion = {'payment_secret': '22' * 32, 'forward_msat': 100000000,
                      'total_msat': 100000000, 'type': 'tlv',
                      'outgoing_cltv_value': 229}

    def test_exact_and_shadow_padded_expiry(self):
        for padding in (0, 6, 12, 100, 1880):
            with self.subTest(padding=padding):
                htlc = dict(self.htlc, cltv_expiry=229 + padding,
                            cltv_expiry_relative=120 + padding)
                self.assertTrue(validate(self.quote, htlc, self.onion, 1000))

    def test_outer_padding_cannot_hide_short_onion_expiry(self):
        onion = dict(self.onion, outgoing_cltv_value=208)  # 99 blocks left
        self.assertEqual(validation_error(self.quote, self.htlc, onion, 1000),
                         'onion CLTV outside quote bounds')

    def test_onion_cannot_outlive_incoming(self):
        onion = dict(self.onion, outgoing_cltv_value=230)
        self.assertFalse(validate(self.quote, self.htlc, onion, 1000))

    def test_outer_bounds(self):
        for remaining in (99, 2001):
            htlc = dict(self.htlc, cltv_expiry=109 + remaining,
                        cltv_expiry_relative=remaining)
            self.assertFalse(validate(self.quote, htlc, self.onion, 1000))

    def test_malformed_expiry(self):
        for value in (None, '229', True):
            self.assertFalse(validate(self.quote, self.htlc,
                                      dict(self.onion, outgoing_cltv_value=value), 1000))

    def test_other_quote_checks_remain_enforced(self):
        for key, value in (('payment_secret', '33' * 32), ('forward_msat', 99999999),
                           ('total_msat', 200000000), ('type', 'legacy'),
                           ('short_channel_id', '1x1x1'), ('next_node_id', 'node')):
            with self.subTest(field=key):
                self.assertFalse(validate(self.quote, self.htlc,
                                          dict(self.onion, **{key: value}), 1000))
        for key, value in (('payment_hash', '33' * 32), ('amount_msat', 99999999)):
            self.assertFalse(validate(self.quote, dict(self.htlc, **{key: value}),
                                      self.onion, 1000))
        self.assertFalse(validate(self.quote, self.htlc, self.onion, 2000))


if __name__ == '__main__':
    unittest.main()
