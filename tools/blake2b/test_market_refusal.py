import unittest
from unittest.mock import patch

import test_reverse_quote_api as fixtures
from reverse_check import DiagnosticError
from quote_refusal import QuoteRefused, market_refusal
from reverse_service import _create
from reverse_request import request_quote
from service_manager import private_load


class MarketTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    creator = fixtures.ApiTests.creator
    start_server = fixtures.ApiTests.start_server

    def refusing_creator(self, settings, invoice, directory, inspector):
        def inspect(*a, **kw):
            raise DiagnosticError('market.validation', ValueError('bid proceeds fall below slippage limit'))
        with patch('reverse_service.enabled'):
            return _create(settings, invoice, directory,
                           rpc=lambda *a: self.fail('Unexpected RPC'), inspector=inspect)

    def test_market_refusal_over_http_then_explicit_retry(self):
        self.api.creator = self.refusing_creator
        server = self.start_server()
        directory = self.root/'customer'
        args = ('lnbc-original', self.credential,
                f'http://127.0.0.1:{server.server_port}', directory, 400000)
        with self.assertRaises(QuoteRefused) as caught:
            request_quote(*args)
        self.assertEqual(caught.exception.reason, 'market_slippage')
        request = private_load(directory/'request.json')
        journal = self.root/'api-requests'/(request['request_id']+'.json')
        self.assertEqual(private_load(journal)['phase'], 'refused')
        self.assertFalse((self.root/('api-'+request['request_id'])).exists())
        self.api.creator = self.creator
        with self.assertRaises(QuoteRefused): request_quote(*args)
        request_quote(*args, retry_refused=True)
        request_quote(*args, retry_refused=True)
        self.assertEqual(self.created, 1)
        self.assertEqual(private_load(directory/'request.json'), request)

    def test_known_market_validators_have_static_codes(self):
        cases = {'limit bid differs from ticker beyond reference gap limit':'market_reference_gap',
                 'insufficient limit-order bid depth':'market_depth',
                 'no limit-order bid liquidity':'market_depth',
                 'crossed or excessive market spread':'market_spread',
                 'stale or future ticker timestamp':'market_stale',
                 'inconsistent ticker and order book':'market_inconsistent'}
        for message, expected in cases.items():
            self.assertEqual(market_refusal(DiagnosticError('market.validation', ValueError(message))), expected)

    def test_other_stages_and_arbitrary_errors_remain_uncertain(self):
        for error in (ValueError('bid proceeds fall below slippage limit'),
                      DiagnosticError('btc.route_planning', ValueError('bid proceeds fall below slippage limit')),
                      DiagnosticError('market.validation', TimeoutError('PRIVATE')),
                      DiagnosticError('market.validation', ValueError('PRIVATE'))):
            self.assertIsNone(market_refusal(error))

    def test_old_uncertain_request_not_reclassified(self):
        self.api.creator = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError('PRIVATE'))
        with self.assertRaises(TimeoutError): self.api.quote(self.body)
        self.api.creator = self.refusing_creator
        with self.assertRaises(ValueError) as caught:
            self.api.quote(dict(self.body, retry_refused=True))
        self.assertNotIsInstance(caught.exception, QuoteRefused)
        path = self.root/'api-requests'/(self.body['request_id']+'.json')
        self.assertEqual(private_load(path)['phase'], 'creating')


if __name__ == '__main__': unittest.main()
