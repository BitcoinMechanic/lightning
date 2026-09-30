"""Small bitcoin-cli-compatible HTTPS adapter for CLN's bcli plugin.

Only the RPCs used by bcli (plus read-only identity checks) are supported.
Credentials and the trusted CA path come exclusively from BTC_RPC_*.
Errors never include server response text, arguments, or endpoint details.
"""
import base64
from decimal import Decimal
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


# Parameter positions parsed as JSON, matching bitcoin-cli's conversion table.
METHODS = {
    'getnetworkinfo': (0, 0, ()),
    'getblockchaininfo': (0, 0, ()),
    'getdeploymentinfo': (0, 0, ()),
    'getmempoolinfo': (0, 0, ()),
    'getpeerinfo': (0, 0, ()),
    'getblockhash': (1, 1, (0,)),
    'getblock': (1, 2, (1,)),
    'getblockheader': (1, 2, (1,)),
    'getblockfrompeer': (2, 2, (1,)),
    'estimatesmartfee': (1, 2, (0,)),
    'gettxout': (2, 3, (1, 2)),
    'sendrawtransaction': (1, 2, (1,)),
}


class RpcError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__('RPC error; server details withheld')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to another endpoint.
        return None


class Client:
    def __init__(self):
        names = ('HOST', 'PORT', 'USER', 'PASSWORD', 'CA')
        values = {n: os.environ['BTC_RPC_' + n] for n in names}
        if any(not v or '\n' in v or '\r' in v for v in values.values()):
            raise ValueError('Invalid environment')
        host = values['HOST']
        if any(c in host for c in '/:@?#[]') or any(c.isspace() for c in host):
            raise ValueError('Use a DNS hostname or IPv4 address only')
        port = int(values['PORT'])
        if not 1 <= port <= 65535:
            raise ValueError('Invalid port')
        self.url = f'https://{host}:{port}/'
        token = base64.b64encode(
            (values['USER'] + ':' + values['PASSWORD']).encode()).decode()
        self.authorization = 'Basic ' + token
        context = ssl.create_default_context(cafile=os.path.expanduser(values['CA']))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=context))

    def call(self, method, params=(), timeout=60):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({'jsonrpc': '1.0', 'id': 1,
                             'method': method, 'params': list(params)}).encode(),
            headers={'Authorization': self.authorization,
                     'Content-Type': 'application/json'})
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in (400, 404, 500):
                raise
            # Bitcoin RPC failures commonly arrive with HTTP 500.
            response = error
        with response:
            reply = json.load(response, parse_float=Decimal)
        if reply.get('id') != 1:
            raise ValueError('Invalid RPC response identity')
        if reply.get('error') is not None:
            code = reply['error']['code']
            if type(code) is not int:
                raise ValueError('Invalid RPC error')
            raise RpcError(code)
        return reply['result']


def parse(args, stdin):
    args = list(args)
    use_stdin, wait, timeout, wait_timeout = False, False, 60, 1
    while args and args[0].startswith('-'):
        option = args.pop(0)
        if option == '-stdin':
            use_stdin = True
        elif option == '-rpcwait':
            wait = True
        elif option.startswith('-rpcclienttimeout='):
            timeout = int(option.split('=', 1)[1])
        elif option.startswith('-rpcwaittimeout='):
            wait_timeout = int(option.split('=', 1)[1])
        else:
            raise ValueError('Unsupported option')
    if not args or args[0] not in METHODS or not 1 <= timeout <= 86400:
        raise ValueError('Unsupported invocation')
    if not 1 <= wait_timeout <= 60:
        raise ValueError('Invalid startup wait')
    method, params = args[0], args[1:]
    if use_stdin:
        params += stdin.read().splitlines()
    minimum, maximum, converted = METHODS[method]
    if not minimum <= len(params) <= maximum:
        raise ValueError('Wrong parameter count')
    params = [json.loads(p) if i in converted else p for i, p in enumerate(params)]
    if wait and method != 'getnetworkinfo':
        raise ValueError('Wait only supported for startup')
    return method, params, timeout, wait, wait_timeout


def encode_result(value):
    """Keep Bitcoin decimal amounts exact and out of exponent notation."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Non-finite amount')
        return format(value, 'f')
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + ':' + encode_result(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(encode_result(v) for v in value) + ']'
    return json.dumps(value, allow_nan=False)


def main():
    try:
        method, params, timeout, wait, seconds = parse(sys.argv[1:], sys.stdin)
        client = Client()
        deadline = time.monotonic() + seconds
        while True:
            try:
                result = client.call(method, params, min(timeout, seconds) if wait else timeout)
                break
            except (RpcError, urllib.error.URLError, TimeoutError, OSError) as error:
                retryable = not isinstance(error, RpcError) or error.code == -28
                if not wait or not retryable or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        # bitcoin-cli emits raw strings and nothing for null (not JSON "null").
        if result is not None:
            print(result if isinstance(result, str) else encode_result(result))
        return 0
    except RpcError as error:
        print(f'RPC error code {error.code}; details withheld', file=sys.stderr)
        return abs(error.code) if 1 <= abs(error.code) <= 255 else 1
    except Exception:
        print('HTTPS RPC adapter failed; private details withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
