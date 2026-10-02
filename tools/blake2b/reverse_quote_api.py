"""Loopback quote API with optional per-quote authorization; no payment submission."""
import argparse
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets

from service_manager import private_load
from swap_controller import save
from reverse_service import create
from quote_refusal import QuoteRefused
from reverse_customer import packet

MAX_BODY = 40000


def token_file(path, payer_id):
    if path.exists():
        value = private_load(path)
        if value.get('payer_id') != payer_id or not re.fullmatch('[0-9a-f]{64}', value.get('token', '')):
            raise ValueError('existing API credential differs')
        return value
    value = dict(token=secrets.token_hex(32), payer_id=payer_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return value


class Quotes:
    def __init__(self, settings, creator=create, inspector=None, auto_process=False):
        self.settings = settings
        self.creator = creator
        if type(auto_process) is not bool:
            raise ValueError('auto_process must be boolean')
        self.auto_process = auto_process
        self.receive = None
        if "receive_config" in settings or "receive_policy" in settings:
            from receive_service import ReceiveQuotes
            self.receive = ReceiveQuotes(settings, auto_process=auto_process)
        from reverse_check import check
        self.inspector = inspector or check
        self.root = Path(settings['swap_root'])
        self.records = self.root/'api-requests'
        if self.records.is_symlink():
            raise ValueError('request directory cannot be a symlink')
        self.records.mkdir(mode=0o700, exist_ok=True)
        if self.records.stat().st_mode & 0o077:
            raise ValueError('request directory must be private')

    def quote(self, request):
        if not isinstance(request, dict):
            raise ValueError('invalid quote request')
        request = dict(request)
        retry_refused = request.pop('retry_refused', False)
        if type(retry_refused) is not bool:
            raise ValueError('invalid retry flag')
        if (set(request) != {'request_id', 'btc_invoice', 'max_xbt_sats'}
                or not isinstance(request['request_id'], str)
                or not re.fullmatch('[0-9a-f]{32}', request['request_id'])
                or not isinstance(request['btc_invoice'], str)
                or not request['btc_invoice'].startswith('lnbc')
                or len(request['btc_invoice']) > 32768
                or any(c.isspace() for c in request['btc_invoice'])
                or type(request['max_xbt_sats']) is not int or not 0 < request['max_xbt_sats'] <= 500000):
            raise ValueError('invalid quote request')
        fd = os.open(self.records/'requests.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._quote(request, retry_refused)
        finally:
            os.close(fd)

    def _quote(self, request, retry_refused=False):
        from reverse_service import binding
        config = binding(self.settings)
        key = request['request_id']
        record = self.records/(key+'.json')
        directory = self.root/('api-'+key)
        if record.exists():
            stored = private_load(record)
            if stored['request'] != request or stored.get('config') != config:
                raise ValueError('request ID reused with different contents')
            if 'offer' in stored:
                return stored['offer']
            if stored.get('phase') == 'refused':
                if directory.exists():
                    raise ValueError('refusal conflicts with quote directory')
                if not retry_refused:
                    raise QuoteRefused(stored['refusal_reason'])
                return self._create_request(stored, record, directory)
            # After a lost reply, only recover an already published quote.
            path = directory/'reverse-quote.json'
            if not path.exists():
                raise ValueError('request outcome requires operator inspection')
            quote = private_load(path)
        else:
            if directory.exists():
                raise ValueError('existing quote directory has no request journal')
            stored = dict(request=request, config=config, phase='creating', auto_process=self.auto_process)
            save(record, stored)  # A retry never creates a second quote.
            return self._create_request(stored, record, directory)
        return self._publish(stored, record, directory, quote)

    def _create_request(self, stored, record, directory):
        request = stored['request']
        stored.update(phase='creating', attempts=stored.get('attempts', 0)+1)
        save(record, stored)  # Lost replies remain uncertain until a definite outcome.
        def inspector(*args, **kwargs):
            result = self.inspector(*args, **kwargs)
            if result['estimated_xbt_sats'] > request['max_xbt_sats']:
                raise QuoteRefused('price_cap')
            return result
        try:
            quote = self.creator(self.settings, request['btc_invoice'], directory, inspector=inspector)
        except QuoteRefused as error:
            if directory.exists():
                raise ValueError('quote creation outcome uncertain') from None
            stored.update(phase='refused', refusal_reason=error.reason)
            save(record, stored)
            raise
        return self._publish(stored, record, directory, quote)

    def _publish(self, stored, record, directory, quote):
        from reverse_service import binding
        request = stored['request']
        if (quote['config'] != binding(self.settings)
                or quote['terms']['btc_invoice'] != request['btc_invoice']):
            raise ValueError('saved quote differs from request or operator')
        offer = packet(quote)
        if offer['xbt_sats'] > request['max_xbt_sats']:
            raise ValueError('saved offer exceeds customer cap')
        if stored.get('auto_process', False):
            from reverse_authorize import authorize
            authorize(directory, self.settings)
        stored.update(phase='quoted', offer=offer)
        save(record, stored)
        return offer


class Server(HTTPServer):
    allow_reuse_address = True
    def __init__(self, port, quotes, credentials):
        if not 1 <= port <= 65535:
            raise ValueError('invalid loopback port')
        self.receive_only = credentials.get('scope') == 'receive'
        self.reverse_only = credentials.get('scope') == 'reverse'
        if self.receive_only:
            if set(credentials) != {'token', 'scope'} or 'receive_policy' not in quotes.settings:
                raise ValueError('receive credential requires invoice-selected policy')
        elif self.reverse_only:
            from reverse_service import binding
            if set(credentials) != {'token', 'scope'} or 'incoming_policy' not in binding(quotes.settings):
                raise ValueError('reverse credential requires unbound incoming policy')
        elif credentials['payer_id'] != quotes.settings['receiver_id']:
            raise ValueError('API token is bound to another customer')
        if not re.fullmatch('[0-9a-f]{64}', credentials['token']):
            raise ValueError('invalid API token')
        self.quotes, self.token = quotes, credentials['token']
        super().__init__(('127.0.0.1', port), Handler)

    def handle_error(self, request, client_address):
        pass  # Never dump request contents or private exception text.


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {'error': 'request_rejected'})

    def do_POST(self):
        auth = self.headers.get_all('Authorization', [])
        if len(auth) != 1 or not hmac.compare_digest(auth[0], 'Bearer '+self.server.token):
            self.reply(401, {'error': 'unauthorized'})
            return
        if self.server.receive_only and self.path != '/v1/receive':
            self.reply(403, {'error': 'request_rejected'})
            return
        if self.server.reverse_only and self.path != '/v1/quote':
            self.reply(403, {'error': 'request_rejected'})
            return
        port = self.server.server_port
        if (self.path not in ('/v1/quote', '/v1/receive') or self.headers.get_all('Origin')
                or self.headers.get_all('Transfer-Encoding')
                or self.headers.get_all('Host', []) != [f'127.0.0.1:{port}']
                or self.headers.get_all('Content-Type', []) != ['application/json']):
            self.reply(400, {'error': 'request_rejected'})
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch('[0-9]{1,6}', lengths[0]) or not 0 < int(lengths[0]) <= MAX_BODY:
            self.reply(400, {'error': 'request_rejected'})
            return
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError()
            request = json.loads(raw)
        except Exception:
            self.reply(400, {'error': 'request_rejected'})
            return
        try:
            handler = self.server.quotes if self.path == '/v1/quote' else self.server.quotes.receive
            if handler is None:
                raise ValueError('receiving API not enabled')
            offer = handler.quote(request)
        except QuoteRefused as error:
            self.reply(409, error.public())
            return
        except Exception:
            self.reply(409, {'error': 'quote_unavailable', 'payment_started': False})
            return
        self.reply(200, offer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'serve'])
    parser.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--port', type=int, default=19840)
    parser.add_argument('--auto-process', action='store_true', help='Authorize automatic processing of new quotes issued by this listener.')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        settings = private_load(args.settings.expanduser())
        if args.command == 'init':
            token_file(args.token_file.expanduser(), settings['receiver_id'])
            print(json.dumps(dict(credentials_ready=True, payment_started=False)))
        else:
            credentials = private_load(args.token_file.expanduser())
            with Server(args.port, Quotes(settings, auto_process=args.auto_process), credentials) as server:
                print(json.dumps(dict(listening='127.0.0.1', port=args.port, auto_process_new_quotes=args.auto_process)), flush=True)
                server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps(dict(event='quote_api_error', details='withheld')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
