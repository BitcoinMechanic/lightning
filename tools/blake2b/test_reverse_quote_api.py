"""Authenticated quote transport and durable request idempotency; no wallets."""
import copy
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from reverse_quote_api import Quotes, Server
from reverse_request import request_quote
from reverse_service import binding
from service_manager import private_load
from swap_controller import save


class ApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = dict(swap_root=str(self.root), btc_cli=['btc'], xbt_cli=['xbt'],
                             node_ids=['btc-id', 'xbt-id'], receiver_id='customer-id')
        self.credential = dict(token='ab'*32, payer_id='customer-id')
        self.body = dict(request_id='11'*16, btc_invoice='lnbc-original', max_xbt_sats=400000)
        self.created = 0
        self.fail_before = False
        self.fail_after = False
        self.api = Quotes(self.settings, creator=self.creator)

    def creator(self, settings, invoice, directory, inspector):
        self.created += 1
        if self.fail_before:
            raise TimeoutError('PRIVATE')
        directory.mkdir()
        quote = dict(config=binding(settings), terms=dict(btc_invoice=invoice, btc_amount_msat=1500000,
            xbt_amount_msat=350000000, expires_at=2000000000), xbt_invoice='lnxbt-offer')
        save(directory/'reverse-quote.json', quote)
        if self.fail_after:
            raise TimeoutError('PRIVATE')
        return quote

    def test_repeat_returns_same_offer_without_new_quote(self):
        first = self.api.quote(self.body)
        second = Quotes(self.settings, creator=self.creator).quote(self.body)
        self.assertEqual(first, second)
        self.assertEqual(self.created, 1)
        self.assertNotIn('config', first)

    def test_changed_customer_binding_cannot_retrieve_cached_offer(self):
        self.api.quote(self.body)
        settings = dict(self.settings, receiver_id='other-customer')
        with self.assertRaises(ValueError):
            Quotes(settings, creator=self.creator).quote(self.body)
        self.assertEqual(self.created, 1)

    def test_changed_request_id_contents_refused(self):
        self.api.quote(self.body)
        for key, value in (('btc_invoice', 'lnbc-other'), ('max_xbt_sats', 300000)):
            with self.assertRaises(ValueError):
                self.api.quote(dict(self.body, **{key: value}))
        self.assertEqual(self.created, 1)

    def test_published_quote_recovered_after_lost_reply(self):
        self.fail_after = True
        with self.assertRaises(TimeoutError):
            self.api.quote(self.body)
        result = self.api.quote(self.body)
        self.assertEqual(result['xbt_sats'], 350000)
        self.assertEqual(self.created, 1)

    def test_unpublished_request_never_recreated(self):
        self.fail_before = True
        with self.assertRaises(TimeoutError):
            self.api.quote(self.body)
        self.fail_before = False
        with self.assertRaises(ValueError):
            self.api.quote(self.body)
        self.assertEqual(self.created, 1)

    def test_paths_caps_and_invalid_invoice_rejected_before_creation(self):
        for key, value in (('request_id', '../outside'), ('max_xbt_sats', True),
                           ('max_xbt_sats', 500001), ('btc_invoice', 'not-invoice')):
            with self.assertRaises(ValueError):
                self.api.quote(dict(self.body, **{key: value}))
        self.assertEqual(self.created, 0)

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = Server(port, self.api, self.credential)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(close)
        return server

    def test_http_auth_origin_paths_and_body_limit_before_quote(self):
        server = self.start_server()
        def post(path='/v1/quote', token='ab'*32, extra=None, length=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            headers = {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}
            if extra:
                headers.update(extra)
            if length:
                headers['Content-Length'] = length
            connection.request('POST', path, body=json.dumps(self.body), headers=headers)
            response = connection.getresponse()
            status, data = response.status, response.read()
            connection.close()
            self.assertNotIn(b'lnbc-original', data)
            self.assertNotIn(b'ababab', data)
            return status
        self.assertEqual(post(token='wrong'), 401)
        self.assertEqual(post(extra={'Origin': 'https://other.invalid'}), 400)
        self.assertEqual(post('/v1/pay'), 400)
        self.assertEqual(post(length='50000'), 400)
        self.assertEqual(self.created, 0)
        self.assertEqual(post(), 200)
        self.assertEqual(post(), 200)
        self.assertEqual(self.created, 1)

    def test_real_client_transport_receives_private_offer_without_payment(self):
        server = self.start_server()
        result = request_quote('lnbc-original', self.credential,
            f'http://127.0.0.1:{server.server_port}', self.root/'request', 400000)
        self.assertFalse(result['payment_started'])
        self.assertTrue(result['customer_review_required'])
        self.assertEqual(private_load(self.root/'request/offer.json')['xbt_sats'], 350000)

    def test_client_lost_reply_reuses_request_and_never_changes_offer(self):
        calls = []
        def send(url, token, body):
            calls.append(copy.deepcopy(body))
            offer = self.api.quote(body)
            if len(calls) == 1:
                raise TimeoutError('PRIVATE')
            return offer
        args = ('lnbc-original', self.credential, 'http://127.0.0.1:19840', self.root/'request', 400000)
        with self.assertRaises(TimeoutError):
            request_quote(*args, send=send)
        request_quote(*args, send=send)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.created, 1)
        with self.assertRaises(ValueError):
            request_quote('lnbc-other', *args[1:], send=send)

    def test_client_refuses_remote_or_credential_bearing_urls(self):
        for url in ('http://example.org:19840', 'http://user:pass@127.0.0.1:19840',
                    'http://127.0.0.1:19840/path', 'https://127.0.0.1:19840'):
            with self.assertRaises(ValueError):
                request_quote('lnbc-original', self.credential, url, self.root/'request', 400000)
        self.assertFalse((self.root/'request').exists())


if __name__ == '__main__':
    unittest.main()
