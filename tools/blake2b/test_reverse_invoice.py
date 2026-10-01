"""Reverse invoice network scope and forward serialization compatibility."""
import hashlib
import unittest
from unittest.mock import patch

from swap_invoice import unsigned_invoice


class InvoiceTests(unittest.TestCase):
    def test_existing_btc_encodings_unchanged(self):
        # Hashes of the pre-0057 helper output, at this fixed timestamp.
        expected = {
            'bc': 'c8c448bb586f5c4d8db65e4efe887edbc7dce674b14c9d9f7a206ec478d971fa',
            'bcrt': '2555baed5e6a59d1eb2a55ec344d51a93538264b78fb13c35145f48d1dcfe9cd',
        }
        with patch('swap_invoice.time.time', return_value=1700000000):
            for currency, digest in expected.items():
                result = unsigned_invoice('11'*32, '22'*32, currency=currency)
                self.assertEqual(hashlib.sha256(result.encode()).hexdigest(), digest)

    def test_live_reverse_network_stays_disabled(self):
        for currency in ('xbt', 'tb', 'unknown'):
            with self.assertRaises(ValueError):
                unsigned_invoice('11'*32, '22'*32, currency=currency)
        self.assertTrue(unsigned_invoice('11'*32, '22'*32, currency='xbtrt',
                                         amount_msat=200000000).startswith('lnxbtrt2000000000p1'))


if __name__ == '__main__':
    unittest.main()
