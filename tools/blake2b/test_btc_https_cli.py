"""Offline contract tests: no real RPC endpoint or funds involved."""
import contextlib
import hashlib
import io
import json
import unittest
from unittest.mock import Mock, patch
import urllib.error

import btc_https_cli as cli
from live_btc_node import check_backend


class HttpsTests(unittest.TestCase):
    def invoke(self, result=None, error=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, 'Client') as factory, \
                patch.object(cli.sys, 'argv', ['adapter', 'gettxout', 'abcd', '0']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            factory.return_value.call.return_value = result
            factory.return_value.call.side_effect = error
            status = cli.main()
        return status, out.getvalue(), err.getvalue()

    def test_stdin_types(self):
        parsed = cli.parse(['-rpcclienttimeout=60', '-stdin', 'getblock'],
                           io.StringIO('abc123\n0\n'))
        self.assertEqual(parsed[:3], ('getblock', ['abc123', 0], 60))
        self.assertEqual(cli.parse(['gettxout', 'tx', '2', 'false'], io.StringIO())[1],
                         ['tx', 2, False])
        self.assertEqual(cli.parse(['sendrawtransaction', 'deadbeef', '0'], io.StringIO())[1],
                         ['deadbeef', 0])
        self.assertEqual(cli.parse(['estimatesmartfee', '6', 'CONSERVATIVE'], io.StringIO())[1],
                         [6, 'CONSERVATIVE'])

    def test_startup_flags(self):
        parsed = cli.parse(['-rpcclienttimeout=60', '-rpcwait', '-rpcwaittimeout=1',
                            'getnetworkinfo'], io.StringIO())
        self.assertEqual(parsed, ('getnetworkinfo', [], 60, True, 1))

    def test_exact_decimal_amounts(self):
        data = json.loads('{"feerate":0.00001000,"value":0.00000001}',
                          parse_float=cli.Decimal)
        status, out, err = self.invoke(data)
        self.assertEqual(status, 0)
        self.assertEqual(out, '{"feerate":0.00001000,"value":0.00000001}\n')
        self.assertEqual(err, '')

    def test_refuse_overrides_and_unknown_methods(self):
        for args in (['-rpcconnect=elsewhere', 'getnetworkinfo'],
                     ['-rpcpassword=secret', 'getnetworkinfo'],
                     ['-regtest', 'getnetworkinfo'], ['stop'],
                     ['getblockhash'], ['-rpcwait', 'sendrawtransaction', '00']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                cli.parse(args, io.StringIO())

    def test_output_contract_and_error_codes(self):
        self.assertEqual(self.invoke(None), (0, '', ''))
        self.assertEqual(self.invoke('abc'), (0, 'abc\n', ''))
        status, out, err = self.invoke(error=cli.RpcError(-27))
        self.assertEqual((status, out), (27, ''))
        self.assertIn('-27', err)
        status, out, err = self.invoke(error=ValueError('PRIVATE_DATA'))
        self.assertEqual((status, out), (1, ''))
        self.assertNotIn('PRIVATE_DATA', err)

    def test_http_500_rpc_error_and_no_redirect(self):
        client = object.__new__(cli.Client)
        client.url = 'https://example.invalid:443/'
        client.authorization = 'Basic test'
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            client.url, 500, 'failure', {}, io.BytesIO(json.dumps({
                'id': 1, 'result': None,
                'error': {'code': -5, 'message': 'PRIVATE_DATA'},
            }).encode()))
        with self.assertRaises(cli.RpcError) as caught:
            client.call('getblock', ['abc', 0])
        self.assertEqual(caught.exception.code, -5)
        self.assertNotIn('PRIVATE_DATA', str(caught.exception))
        self.assertIsNone(cli.NoRedirect().redirect_request(None, None, 302, '', {},
                                                           'https://other.invalid'))

    def test_explicit_ca_and_no_proxy(self):
        env = {'BTC_RPC_HOST': 'node.example', 'BTC_RPC_PORT': '443',
               'BTC_RPC_USER': 'user', 'BTC_RPC_PASSWORD': 'secret',
               'BTC_RPC_CA': '/local/ca.pem'}
        with patch.dict(cli.os.environ, env), \
                patch.object(cli.ssl, 'create_default_context') as context, \
                patch.object(cli.urllib.request, 'build_opener'), \
                patch.object(cli.urllib.request, 'ProxyHandler') as proxy:
            cli.Client()
            context.assert_called_once_with(cafile='/local/ca.pem')
            proxy.assert_called_once_with({})

    def test_backend_identity(self):
        header = bytes(80)
        block_hash = hashlib.sha256(hashlib.sha256(header).digest()).digest()[::-1].hex()
        genesis = '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'
        info = {'chain': 'main', 'initialblockdownload': False, 'pruned': False,
                'blocks': 970000, 'bestblockhash': block_hash}
        def call(method, params=()):
            if method == 'getblockchaininfo':
                return info
            if method == 'getblockhash':
                return genesis if params == [0] else block_hash
            return header.hex()
        client = Mock()
        client.call.side_effect = call
        check_backend(client)
        info['chain'] = 'regtest'
        with self.assertRaises(ValueError):
            check_backend(client)
        info['chain'] = 'main'
        header = bytes(164)
        with self.assertRaises(ValueError):
            check_backend(client)


if __name__ == '__main__':
    unittest.main()
