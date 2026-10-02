"""Public diagnostics never turn uncertain quote outcomes into safe retries."""
import io
import json
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

from customer_errors import CustomerError, channel_reason
from quote_refusal import QuoteRefused
from reverse_request import transport
from reverse_check import CheckError
from reverse_service import _create
from service_manager import private_load
import test_receive_api as forward
import test_reverse_quote_api as reverse


class ErrorTests(unittest.TestCase):
    def channel(self, **changes):
        return dict(dict(state='CHANNELD_NORMAL', peer_connected=True, htlcs=[],
                         spendable_msat=1000, receivable_msat=1000), **changes)

    def test_channel_roles_readiness_and_liquidity(self):
        for role in ('btc', 'xbt'):
            self.assertEqual(channel_reason([], role), role+'_channel_unavailable')
            self.assertEqual(channel_reason([self.channel(peer_connected=False)], role), role+'_peer_disconnected')
            self.assertEqual(channel_reason([self.channel(htlcs=[{}])], role), role+'_channel_busy')
            self.assertEqual(channel_reason([self.channel()], role, 1001), 'insufficient_'+role+'_send_liquidity')
            self.assertEqual(channel_reason([self.channel()], role, 1001, 'receivable_msat'), 'insufficient_'+role+'_receive_liquidity')
            self.assertIsNone(channel_reason([self.channel()], role, 1000))

    def test_malformed_channel_data_is_not_public_refusal(self):
        for channel in (self.channel(peer_connected='false'), self.channel(htlcs='PRIVATE'),
                        self.channel(spendable_msat=-1)):
            with self.assertRaises(ValueError) as caught:
                channel_reason([channel], 'btc', 1)
            self.assertNotIsInstance(caught.exception, QuoteRefused)

    def transport_error(self, error):
        opener = MagicMock()
        opener.open.side_effect = error
        with patch('reverse_request.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(CustomerError) as caught:
                transport('http://127.0.0.1:1', 'token', {})
        return caught.exception.public()

    def test_connection_refused_and_credentials_have_static_messages(self):
        errors = [(urllib.error.URLError(ConnectionRefusedError(111, 'PRIVATE')), 'api_unreachable'),
                  (urllib.error.HTTPError('PRIVATE', 401, 'PRIVATE', {}, io.BytesIO(b'PRIVATE')), 'api_credentials')]
        for error, code in errors:
            answer = self.transport_error(error)
            self.assertEqual(answer['reason'], code)
            self.assertNotIn('PRIVATE', json.dumps(answer))
            self.assertNotIn('payment_started', answer)
            self.assertFalse(answer['automatic_resubmission'])

    def test_lost_reply_and_unknown_http_remain_uncertain(self):
        errors = [TimeoutError('PRIVATE'), urllib.error.URLError(TimeoutError('PRIVATE')),
                  urllib.error.HTTPError('PRIVATE', 503, 'PRIVATE', {}, io.BytesIO(b'PRIVATE')),
                  urllib.error.HTTPError('PRIVATE', 409, 'PRIVATE', {}, io.BytesIO(b'{"reason":"PRIVATE"}'))]
        for error in errors:
            answer = self.transport_error(error)
            self.assertEqual(answer['reason'], 'api_outcome_unknown')
            self.assertNotIn('PRIVATE', json.dumps(answer))
            self.assertNotIn('quote_created', answer)

    def test_static_refusal_survives_transport(self):
        body = json.dumps(QuoteRefused('btc_peer_disconnected').public()).encode()
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('url', 409, '', {}, io.BytesIO(body))
        with patch('reverse_request.urllib.request.build_opener', return_value=opener), self.assertRaises(QuoteRefused) as caught:
            transport('http://127.0.0.1:1', 'token', {})
        self.assertEqual(caught.exception.reason, 'btc_peer_disconnected')

    def test_invalid_success_json_remains_uncertain(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'PRIVATE'
        with patch('reverse_request.urllib.request.build_opener', return_value=opener), self.assertRaises(CustomerError) as caught:
            transport('http://127.0.0.1:1', 'token', {})
        self.assertEqual(caught.exception.reason, 'api_outcome_unknown')


class ForwardTests(unittest.TestCase):
    setUp = forward.ReceiveTests.setUp
    create = forward.ReceiveTests.create
    publish = forward.ReceiveTests.publish
    directory = forward.ReceiveTests.directory

    def test_precreation_refusal_is_recorded_and_explicit_retry_reuses_request(self):
        with patch('receive_service.service.create', side_effect=QuoteRefused('xbt_peer_disconnected')):
            with self.assertRaises(QuoteRefused):
                self.api.quote(self.body)
        record = self.root/'receive-requests'/(self.body['request_id']+'.json')
        self.assertEqual(private_load(record)['phase'], 'refused')
        self.assertFalse(self.directory().exists())
        with self.assertRaises(QuoteRefused):
            self.api.quote(self.body)
        offer = self.api.quote(dict(self.body, retry_refused=True))
        self.assertEqual(self.api.quote(self.body), offer)
        self.assertEqual(self.created, 1)

    def test_error_after_directory_creation_never_becomes_retryable(self):
        def ambiguous(config, invoice, amount, directory):
            directory.mkdir(mode=0o700)
            raise QuoteRefused('xbt_peer_disconnected')
        with patch('receive_service.service.create', side_effect=ambiguous):
            with self.assertRaises(ValueError) as caught:
                self.api.quote(self.body)
            self.assertNotIsInstance(caught.exception, QuoteRefused)
        record = self.root/'receive-requests'/(self.body['request_id']+'.json')
        self.assertEqual(private_load(record)['phase'], 'creating')
        with patch('receive_service.service.create') as creator, self.assertRaises(Exception):
            self.api.quote(dict(self.body, retry_refused=True))
        creator.assert_not_called()


class ReverseTests(unittest.TestCase):
    setUp = reverse.ApiTests.setUp
    creator = reverse.ApiTests.creator

    def test_typed_read_only_failure_is_public_before_mutations(self):
        for code in ('btc_peer_disconnected', 'xbt_channel_busy', 'invoice_expiring'):
            def inspector(*a, **kw):
                raise CheckError('PRIVATE', code)
            with patch('reverse_service.enabled'), self.assertRaises(QuoteRefused) as caught:
                _create(self.settings, 'invoice', self.root/'new',
                        inspector=inspector, rpc=lambda *a: self.fail('Unexpected RPC'))
            self.assertEqual(caught.exception.reason, code)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertFalse((self.root/'new').exists())

    def test_unclassified_read_only_failure_keeps_existing_uncertain_handling(self):
        def inspector(*a, **kw):
            raise CheckError('PRIVATE')
        with patch('reverse_service.enabled'), self.assertRaises(CheckError):
            _create(self.settings, 'invoice', self.root/'new', inspector=inspector)


if __name__ == '__main__':
    unittest.main()
