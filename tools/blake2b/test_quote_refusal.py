"""Definite refusal retries versus ambiguous quote creation outcomes."""
from unittest.mock import patch
import unittest

import test_reverse_quote_api as fixtures
from reverse_request import request_quote
from reverse_service import _create
from quote_refusal import QuoteRefused
from service_manager import private_load


class RefusalTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    creator = fixtures.ApiTests.creator
    start_server = fixtures.ApiTests.start_server

    def refuse(self, *args, **kwargs):
        self.created += 1
        raise QuoteRefused('insufficient_xbt_liquidity')

    def test_definite_refusal_cached_until_explicit_retry(self):
        self.api.creator = self.refuse
        with self.assertRaises(QuoteRefused):
            self.api.quote(self.body)
        record = self.root/'api-requests'/(self.body['request_id']+'.json')
        self.assertEqual(private_load(record)['phase'], 'refused')
        self.api.creator = self.creator
        with self.assertRaises(QuoteRefused):
            self.api.quote(self.body)
        self.assertEqual(self.created, 1)
        offer = self.api.quote(dict(self.body, retry_refused=True))
        self.assertEqual(self.created, 2)
        self.assertEqual(offer, self.api.quote(dict(self.body, retry_refused=True)))
        self.assertEqual(self.created, 2)

    def test_unknown_outcome_not_retryable(self):
        self.fail_before = True
        with self.assertRaises(TimeoutError):
            self.api.quote(self.body)
        self.fail_before = False
        with self.assertRaises(ValueError):
            self.api.quote(dict(self.body, retry_refused=True))
        self.assertEqual(self.created, 1)

    def test_refusal_after_directory_creation_remains_uncertain(self):
        def partial(settings, invoice, directory, inspector):
            directory.mkdir()
            raise QuoteRefused('price_cap')
        self.api.creator = partial
        with self.assertRaises(ValueError):
            self.api.quote(self.body)
        record = self.root/'api-requests'/(self.body['request_id']+'.json')
        self.assertEqual(private_load(record)['phase'], 'creating')
        with self.assertRaises(ValueError):
            self.api.quote(dict(self.body, retry_refused=True))

    def test_real_http_refusal_then_retry_same_customer_request(self):
        self.api.creator = self.refuse
        server = self.start_server()
        args = ('lnbc-original', self.credential,
                f'http://127.0.0.1:{server.server_port}', self.root/'request', 400000)
        with self.assertRaises(QuoteRefused) as caught:
            request_quote(*args)
        self.assertEqual(caught.exception.reason, 'insufficient_xbt_liquidity')
        self.assertNotIn('lnbc', str(caught.exception))
        original = private_load(self.root/'request/request.json')
        self.api.creator = self.creator
        answer = request_quote(*args, retry_refused=True)
        self.assertTrue(answer['quote_received'])
        self.assertEqual(original, private_load(self.root/'request/request.json'))
        request_quote(*args, retry_refused=True)
        self.assertEqual(self.created, 2)

    def test_retry_rejects_changed_invoice(self):
        self.api.creator = self.refuse
        with self.assertRaises(QuoteRefused):
            self.api.quote(self.body)
        with self.assertRaises(ValueError):
            self.api.quote(dict(self.body, btc_invoice='lnbc-changed', retry_refused=True))
        self.assertEqual(self.created, 1)

    def test_read_only_preflight_maps_liquidity_before_quote_directory(self):
        directory = self.root/'preflight'
        summary = dict(route_found=True, reasons=['insufficient XBT payer-to-operator liquidity'])
        with patch('reverse_service.enabled'), self.assertRaises(QuoteRefused) as caught:
            _create(self.settings, 'lnbc-original', directory,
                    rpc=lambda *a: self.fail('Unexpected RPC'),
                    inspector=lambda *a, **k: summary)
        self.assertEqual(caught.exception.reason, 'insufficient_xbt_liquidity')
        self.assertFalse(directory.exists())

    def test_bad_retry_type_refused_before_creation(self):
        with self.assertRaises(ValueError):
            self.api.quote(dict(self.body, retry_refused='yes'))
        self.assertEqual(self.created, 0)


if __name__ == '__main__':
    unittest.main()
