"""Create an own-wallet XBT invoice and obtain a bounded BTC invoice over the API."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from customer_errors import CustomerError
from quote_refusal import QuoteRefused
from receive_service import FORMAT, FIELDS
from reverse_customer import locked
from reverse_request import transport
from service_manager import private_load
from swap_controller import save
from swap_rpc import RPC


def validate(offer, invoice, amount, cap, cli, rpc, now):
    if (not isinstance(offer, dict) or set(offer) != FIELDS or offer['format'] != FORMAT
            or offer['xbt_invoice_sha256'] != hashlib.sha256(invoice.encode()).hexdigest()
            or type(offer['xbt_sats']) is not int or offer['xbt_sats'] != amount
            or type(offer['btc_sats']) is not int or not 0 < offer['btc_sats'] <= cap
            or type(offer['expires_at']) is not int
            or not isinstance(offer['btc_invoice'], str) or not offer['btc_invoice'].startswith('lnbc')
            or len(offer['btc_invoice']) > 32768):
        raise ValueError('receiving offer differs from request')
    own, btc = [rpc(cli, 'decode', value) for value in (invoice, offer['btc_invoice'])]
    for decoded, currency, sats in ((own, 'xbt', amount), (btc, 'bc', offer['btc_sats'])):
        if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
                or decoded.get('currency') != currency or decoded.get('amount_msat') != sats*1000
                or type(decoded.get('created_at')) is not int or type(decoded.get('expiry')) is not int
                or decoded['created_at']+decoded['expiry'] < offer['expires_at']):
            raise ValueError('signed invoice mismatch')
    if (btc.get('payment_hash') != own.get('payment_hash') or not own.get('payment_hash')
            or type(btc.get('min_final_cltv_expiry')) is not int
            or not 0 < btc['min_final_cltv_expiry'] <= 2016):
        raise ValueError('invoice hash or timelock mismatch')
    if offer['expires_at'] <= int(now()):
        raise ValueError('receiving offer expired')


def workflow(cli, directory, credentials, url, xbt_sats, max_btc_sats,
             retry_quote=False, rpc=RPC.call, send=transport, now=time.time):
    if (type(xbt_sats) is not int or not 0 < xbt_sats <= 500000
            or type(max_btc_sats) is not int or not 0 < max_btc_sats <= 10000
            or type(retry_quote) is not bool):
        raise ValueError('invalid receiving bounds')
    endpoint = urlsplit(url)
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
            or endpoint.username or endpoint.password or endpoint.path or endpoint.query
            or endpoint.fragment or not endpoint.port or not 1 <= endpoint.port <= 65535):
        raise ValueError('explicit loopback endpoint required')
    token = credentials['token']
    if not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('invalid credential')
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('private receiving directory required')
    with locked(directory):
        info = rpc(cli, 'getinfo')
        if (info['network'] != 'xbt' or info['id'] != credentials['payer_id']
                or any(k.startswith('warning_') for k in info)):
            raise ValueError('customer wallet mismatch or warning')
        expected = dict(cli=cli, customer_id=info['id'], endpoint=url, xbt_sats=xbt_sats,
                        max_btc_sats=max_btc_sats, credential_sha256=hashlib.sha256(token.encode()).hexdigest())
        path = directory/'receive.json'
        if path.exists():
            state = private_load(path)
            if any(state.get(k) != v for k, v in expected.items()):
                raise ValueError('receiving intent changed')
        else:
            if any(p.name != 'customer.lock' for p in directory.iterdir()):
                raise ValueError('new receiving directory must be empty')
            state = dict(expected, request_id=secrets.token_hex(16), label='receive-api-'+secrets.token_hex(16))
            save(path, state)  # Persist the label before the invoice RPC.
        found = rpc(cli, 'listinvoices', state['label'])['invoices']
        if len(found) > 1:
            raise ValueError('ambiguous wallet invoice')
        if not found:
            if 'xbt_invoice' in state:
                raise ValueError('saved wallet invoice missing')
            rpc(cli, 'invoice', str(xbt_sats*1000)+'msat', state['label'], 'Receive XBT through BTC swap', 3600)
            found = rpc(cli, 'listinvoices', state['label'])['invoices']
        if len(found) != 1 or found[0]['amount_msat'] != xbt_sats*1000:
            raise ValueError('wallet invoice amount differs')
        invoice = found[0]
        if 'xbt_invoice' in state and state['xbt_invoice'] != invoice['bolt11']:
            raise ValueError('wallet invoice changed')
        if 'xbt_invoice' not in state:
            state['xbt_invoice'] = invoice['bolt11']
            save(path, state)
        if invoice['status'] == 'paid':
            return dict(outcome='paid', received_xbt_sats=invoice['amount_received_msat']//1000)
        if invoice['status'] != 'unpaid' or invoice['expires_at'] <= int(now()):
            return dict(outcome='invoice_expired', automatic_requote=False)
        if 'offer' in state and state['offer']['expires_at'] <= int(now()):
            return dict(outcome='quote_expired', automatic_requote=False)
        if 'offer' not in state:
            request = dict(request_id=state['request_id'], xbt_invoice=state['xbt_invoice'], max_btc_sats=max_btc_sats)
            if retry_quote:
                request['retry_refused'] = True
            offer = send(url, token, request, endpoint_path='/v1/receive')
            validate(offer, state['xbt_invoice'], xbt_sats, max_btc_sats, cli, rpc, now)
            state['offer'] = offer
            save(path, state)
        offer = state['offer']
        validate(offer, state['xbt_invoice'], xbt_sats, max_btc_sats, cli, rpc, now)
        return dict(outcome='awaiting_btc', btc_sats=offer['btc_sats'], xbt_sats=xbt_sats,
                    expires_at=offer['expires_at'], btc_invoice=offer['btc_invoice'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lightning-dir', type=Path, required=True)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--xbt-sats', type=int, required=True)
    p.add_argument('--max-btc-sats', type=int, required=True)
    p.add_argument('--token-file', type=Path, default=Path.home()/'.config/cln-swaps/customer-api.json')
    p.add_argument('--url', default='http://127.0.0.1:19840')
    p.add_argument('--retry-quote', action='store_true')
    a = p.parse_args()
    os.umask(0o077)
    try:
        cli = [str(Path(__file__).resolve().parents[2]/'cli/lightning-cli'),
               '--lightning-dir='+str(a.lightning_dir.expanduser().resolve()),
               '--network=xbt', '--json', '--notifications=none']
        print(json.dumps(workflow(cli, a.directory.expanduser().absolute(), private_load(a.token_file.expanduser()),
                                  a.url, a.xbt_sats, a.max_btc_sats, retry_quote=a.retry_quote)))
        return 0
    except CustomerError as error:
        print(json.dumps(error.public()))
        return 1
    except QuoteRefused as error:
        print(json.dumps(dict(error.public(), message=str(error), next_step='Correct the cause; rerun the same command with --retry-quote.')))
        return 1
    except (Exception, KeyboardInterrupt):
        print(json.dumps(dict(event='receive_workflow_interrupted', details='withheld',
                             next_step='Preserve this directory and rerun the same command to inspect or resume.')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
