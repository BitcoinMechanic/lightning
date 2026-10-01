"""Request a bounded quote from the authenticated loopback API. Never pays."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import urllib.request
import urllib.error
from quote_refusal import QuoteRefused, REASONS
from urllib.parse import urlsplit

from service_manager import private_load
from reverse_check import private_invoice
from reverse_customer import FIELDS, FORMAT, locked
from swap_controller import save


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('quote endpoint redirect refused')


def transport(url, token, body):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url+'/v1/quote', data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=90) as response:
            raw = response.read(65537)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            raw = error.read(4097)
            try:
                value = json.loads(raw) if len(raw) <= 4096 else None
            except (ValueError, UnicodeError):
                value = None
            if (isinstance(value, dict) and set(value) == {'error', 'reason', 'quote_created', 'payment_started'}
                    and value['error'] == 'quote_refused' and isinstance(value['reason'], str)
                    and value['reason'] in REASONS and value['quote_created'] is False
                    and value['payment_started'] is False):
                raise QuoteRefused(value['reason']) from None
        raise ValueError('quote request outcome unavailable') from None
    if len(raw) > 65536:
        raise ValueError('quote response too large')
    return json.loads(raw)


def request_quote(invoice, credentials, url, directory, max_xbt_sats, send=transport, retry_refused=False):
    if type(retry_refused) is not bool:
        raise ValueError('invalid retry flag')
    endpoint = urlsplit(url)
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.path or endpoint.query or endpoint.fragment
            or not endpoint.port or not 1 <= endpoint.port <= 65535):
        raise ValueError('use an explicit loopback HTTP endpoint and port')
    token = credentials['token']
    if (not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token)
            or type(max_xbt_sats) is not int or not 0 < max_xbt_sats <= 500000):
        raise ValueError('invalid customer credential or price cap')
    expected = dict(btc_invoice=invoice, max_xbt_sats=max_xbt_sats, endpoint=url,
                    credential_sha256=hashlib.sha256(token.encode()).hexdigest())
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('customer request directory must be private')
    with locked(directory):
        path = directory/'request.json'
        if path.exists():
            stored = private_load(path)
            if any(stored.get(k) != v for k, v in expected.items()):
                raise ValueError('existing request differs')
        else:
            if any(p.name != 'customer.lock' for p in directory.iterdir()):
                raise ValueError('new request directory must be empty')
            stored = dict(expected, request_id=secrets.token_hex(16))
            save(path, stored)
        body = {k: stored[k] for k in ('request_id', 'btc_invoice', 'max_xbt_sats')}
        if retry_refused:
            body['retry_refused'] = True
        offer = send(url, token, body)
        if (not isinstance(offer, dict) or set(offer) != FIELDS or offer['format'] != FORMAT
                or offer['btc_invoice_sha256'] != hashlib.sha256(invoice.encode()).hexdigest()
                or type(offer['xbt_sats']) is not int or not 0 < offer['xbt_sats'] <= max_xbt_sats
                or type(offer['btc_sats']) is not int or offer['btc_sats'] != 1500
                or type(offer['expires_at']) is not int
                or not isinstance(offer['xbt_invoice'], str) or len(offer['xbt_invoice']) > 32768):
            raise ValueError('quote response differs from request')
        output = directory/'offer.json'
        if output.exists():
            if private_load(output) != offer:
                raise ValueError('operator changed an existing offer')
        else:
            save(output, offer)
        return dict(quote_received=True, customer_review_required=True,
                    btc_sats=offer['btc_sats'], xbt_sats=offer['xbt_sats'],
                    expires_at=offer['expires_at'], expired=offer['expires_at'] <= int(time.time()),
                    payment_started=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invoice-file', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--url', default='http://127.0.0.1:19840')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--max-xbt-sats', type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = request_quote(private_invoice(args.invoice_file.expanduser()),
            private_load(args.token_file.expanduser()), args.url,
            args.directory.expanduser().absolute(), args.max_xbt_sats)
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(event='quote_request_error', details='withheld', payment_started=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
