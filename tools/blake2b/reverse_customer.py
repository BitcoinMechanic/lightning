"""Customer-owned quote review and single-submission XBT payment, using own RPC only.

Input/output bundles are private files for now. No operator RPC, automatic quote
acceptance, automatic resubmission or public network server is provided here.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from service_manager import private_load
from reverse_check import private_invoice
from reverse_metadata import invoice_metadata
from swap_controller import save
from swap_rpc import RPC

FORMAT = 'xbt-btc-customer-offer-v1'
FIELDS = {'format', 'btc_invoice_sha256', 'xbt_invoice', 'btc_sats', 'xbt_sats', 'expires_at'}


def packet(quote):
    terms = quote['terms']
    return dict(format=FORMAT,
        btc_invoice_sha256=hashlib.sha256(terms['btc_invoice'].encode()).hexdigest(),
        xbt_invoice=quote['xbt_invoice'], btc_sats=terms['btc_amount_msat']//1000,
        xbt_sats=terms['xbt_amount_msat']//1000, expires_at=terms['expires_at'])


def export(directory, output):
    quote = private_load(directory/'reverse-quote.json')
    value = packet(quote)
    if output.exists():
        if private_load(output) != value:
            raise ValueError('existing customer offer differs')
    else:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
    return dict(exported=True, btc_sats=value['btc_sats'], xbt_sats=value['xbt_sats'],
                expires_at=value['expires_at'], payment_started=False)


def routing_budget(max_xbt_sats, max_xbt_routing_fee_sats):
    """None preserves legacy direct-only mode; explicit zero permits free routing."""
    if (type(max_xbt_sats) is not int or not 0 < max_xbt_sats <= 500000
            or (max_xbt_routing_fee_sats is not None
                and (type(max_xbt_routing_fee_sats) is not int
                     or not 0 <= max_xbt_routing_fee_sats <= 1000
                     or max_xbt_routing_fee_sats >= max_xbt_sats))):
        raise ValueError('invalid customer routing budget')
    return 0 if max_xbt_routing_fee_sats is None else max_xbt_routing_fee_sats


def fee_summary(offer, fee):
    if fee is None:
        return {}
    return dict(max_xbt_routing_fee_sats=fee,
                max_total_xbt_sats=offer['xbt_sats']+fee)


def validate(offer, invoice, cli, max_xbt_sats, max_delay, rpc=RPC.call, now=time.time,
             network='xbt', max_xbt_routing_fee_sats=None):
    fee = routing_budget(max_xbt_sats, max_xbt_routing_fee_sats)
    if network not in ('xbt', 'xbt-regtest'):
        raise ValueError('unsupported customer network')
    if (set(offer) != FIELDS or offer['format'] != FORMAT
            or not isinstance(offer['xbt_invoice'], str) or len(offer['xbt_invoice']) > 32768
            or offer['btc_invoice_sha256'] != hashlib.sha256(invoice.encode()).hexdigest()
            or type(max_xbt_sats) is not int or not 0 < max_xbt_sats <= 500000
            or type(max_delay) is not int or not 1 <= max_delay <= 2016
            or type(offer['btc_sats']) is not int or offer['btc_sats'] != 1500
            or type(offer['xbt_sats']) is not int or not 0 < offer['xbt_sats'] <= max_xbt_sats
            or offer['xbt_sats'] + fee > max_xbt_sats
            or type(offer['expires_at']) is not int or offer['expires_at'] <= int(now())):
        raise ValueError('customer quote outside approved limits or expired')
    info = rpc(cli, 'getinfo')
    if info['network'] != network or any(k.startswith('warning_') for k in info):
        raise ValueError('customer wallet network or readiness mismatch')
    btc, xbt = [rpc(cli, 'decode', value) for value in (invoice, offer['xbt_invoice'])]
    for decoded, currency, amount in ((btc, 'bc' if network == 'xbt' else 'bcrt', offer['btc_sats']),
                                      (xbt, 'xbt' if network == 'xbt' else 'xbtrt', offer['xbt_sats'])):
        if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
                or decoded.get('currency') != currency or decoded.get('amount_msat') != amount*1000
                or not isinstance(decoded.get('payment_hash'), str)
                or not re.fullmatch('[0-9a-f]{64}', decoded['payment_hash'])
                or type(decoded.get('created_at')) is not int or type(decoded.get('expiry')) is not int
                or decoded['created_at']+decoded['expiry'] < offer['expires_at']
                or not isinstance(decoded.get('payee'), str)
                or not re.fullmatch('0[23][0-9a-f]{64}', decoded['payee'])):
            raise ValueError('signed invoice does not match customer offer')
        invoice_metadata(decoded)
    if (btc['payment_hash'] != xbt['payment_hash'] or xbt['payee'] == info['id']
            or type(xbt.get('min_final_cltv_expiry')) is not int
            or not 0 < xbt['min_final_cltv_expiry'] <= max_delay):
        raise ValueError('invoice hash, recipient or locktime mismatch')
    channels = rpc(cli, 'listpeerchannels')['channels']
    if max_xbt_routing_fee_sats is None:
        channels = [c for c in channels if c.get('peer_id') == xbt['payee']
                    and c.get('state') == 'CHANNELD_NORMAL']
        if (len(channels) != 1 or channels[0].get('peer_connected') is not True
                or channels[0].get('htlcs') or channels[0].get('spendable_msat', 0) < offer['xbt_sats']*1000):
            raise ValueError('customer needs one ready direct channel with sufficient balance')
    elif not any(c.get('state') == 'CHANNELD_NORMAL' and c.get('peer_connected') is True
                 and not c.get('htlcs') and type(c.get('spendable_msat')) is int
                 and c['spendable_msat'] >= (offer['xbt_sats']+fee)*1000 for c in channels):
        raise ValueError('customer needs a ready first-hop channel covering amount and fee cap')
    # This is a local liquidity check, not a promise of a route. The wallet's
    # pay command plans the route and enforces the persisted fee/delay limits.
    return dict(customer_id=info['id'], operator_id=xbt['payee'], payment_hash=xbt['payment_hash'],
                final_cltv=xbt['min_final_cltv_expiry'])


def review(offer, invoice, cli, directory, max_xbt_sats, max_delay=2016, rpc=RPC.call,
           now=time.time, network='xbt', max_xbt_routing_fee_sats=None):
    checked = validate(offer, invoice, cli, max_xbt_sats, max_delay, rpc, now, network,
                       max_xbt_routing_fee_sats)
    directory.mkdir(mode=0o700)  # Exclusive: never replace an existing customer intent.
    options = ({} if max_xbt_routing_fee_sats is None else
               dict(max_xbt_routing_fee_sats=max_xbt_routing_fee_sats))
    save(directory/'customer.json', dict(phase='reviewed', offer=offer, btc_invoice=invoice,
         cli=cli, network=network, max_xbt_sats=max_xbt_sats, max_delay=max_delay, **options, **checked))
    return dict(reviewed=True, btc_sats=offer['btc_sats'], xbt_sats=offer['xbt_sats'],
                final_cltv=checked['final_cltv'], expires_at=offer['expires_at'], payment_started=False,
                **fee_summary(offer, max_xbt_routing_fee_sats))


@contextmanager
def locked(directory):
    fd = os.open(directory/'customer.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def payment_rows(state, rpc):
    info = rpc(state['cli'], 'getinfo')
    if info['network'] != state['network'] or info['id'] != state['customer_id']:
        raise ValueError('customer wallet identity changed')
    rows = rpc([*state['cli'], '-k'], 'listpays', 'payment_hash='+state['payment_hash'])['pays']
    if any(p.get('payment_hash') != state['payment_hash'] for p in rows):
        raise ValueError('payment query mismatch')
    return rows


def result(state, rpc=RPC.call):
    fee = routing_budget(state['max_xbt_sats'], state.get('max_xbt_routing_fee_sats'))
    rows = payment_rows(state, rpc)
    if not rows:
        return dict(outcome='not_submitted' if state['phase'] == 'reviewed' else 'unknown',
                    automatic_resubmission=False)
    if len(rows) != 1:
        raise ValueError('ambiguous customer payment records')
    row = rows[0]
    if row.get('bolt11') != state['offer']['xbt_invoice'] or row.get('destination') != state['operator_id']:
        raise ValueError('customer payment record differs')
    outcome = row.get('status')
    if outcome not in ('pending', 'failed', 'complete'):
        raise ValueError('unknown customer payment status')
    answer = dict(outcome=outcome, automatic_resubmission=False)
    if outcome == 'complete':
        preimage = bytes.fromhex(row['preimage'])
        if (len(preimage) != 32 or hashlib.sha256(preimage).hexdigest() != state['payment_hash']
                or row.get('amount_msat') != state['offer']['xbt_sats']*1000
                or type(row.get('amount_sent_msat')) is not int
                or not row['amount_msat'] <= row['amount_sent_msat'] <= row['amount_msat']+fee*1000
                or row['amount_sent_msat'] > state['max_xbt_sats']*1000):
            raise ValueError('customer payment proof or amount differs')
        answer.update(btc_invoice_sats=state['offer']['btc_sats'], xbt_sent_sats=row['amount_sent_msat']//1000,
                      matching_preimage_verified=True)
        if state.get('max_xbt_routing_fee_sats') is not None:
            answer.update(xbt_sent_msat=row['amount_sent_msat'],
                          xbt_routing_fee_msat=row['amount_sent_msat']-row['amount_msat'])
    return answer


def pay(directory, rpc=RPC.call, now=time.time):
    with locked(directory):
        path = directory/'customer.json'
        state = private_load(path)
        if state['phase'] == 'submitted':
            return result(state, rpc)  # Never call pay again, even after failure or missing records.
        if state['phase'] != 'reviewed':
            raise ValueError('unexpected customer state')
        checked = validate(state['offer'], state['btc_invoice'], state['cli'], state['max_xbt_sats'],
                           state['max_delay'], rpc, now, state['network'], state.get('max_xbt_routing_fee_sats'))
        if any(state[k] != value for k, value in checked.items()) or payment_rows(state, rpc):
            raise ValueError('review binding changed or payment already recorded')
        fee = routing_budget(state['max_xbt_sats'], state.get('max_xbt_routing_fee_sats'))
        state['phase'] = 'submitted'
        save(path, state)  # Submission intent is durable BEFORE the pay RPC.
        try:
            rpc([*state['cli'], '-k'], 'pay', 'bolt11='+state['offer']['xbt_invoice'],
                'maxfee='+str(fee*1000)+'msat', 'maxdelay='+str(state['max_delay']), 'retry_for=0')
        except Exception:
            pass  # Lost replies are reconciled by reads, never by another submission.
        return result(state, rpc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('review')
    p.add_argument('--offer-file', type=Path, required=True)
    p.add_argument('--btc-invoice-file', type=Path, required=True)
    p.add_argument('--lightning-dir', type=Path, required=True)
    p.add_argument('--max-xbt-sats', type=int, required=True)
    p.add_argument('--max-delay', type=int, default=2016)
    p.add_argument('--max-xbt-routing-fee-sats', type=int, default=None,
                   help='Opt into routing; fee cap included in --max-xbt-sats (0..1000).')
    p.add_argument('--directory', type=Path, required=True)
    for name in ('pay', 'status'):
        p = sub.add_parser(name)
        p.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = args.directory.expanduser().resolve()
        if args.command == 'review':
            path = args.offer_file.expanduser()
            if path.stat().st_size > 65536:
                raise ValueError('offer too large')
            cli = [str(Path(__file__).resolve().parents[2]/'cli/lightning-cli'),
                   '--lightning-dir='+str(args.lightning_dir.expanduser().resolve()),
                   '--network=xbt', '--json', '--notifications=none']
            answer = review(private_load(path), private_invoice(args.btc_invoice_file.expanduser()),
                            cli, directory, args.max_xbt_sats, args.max_delay,
                            max_xbt_routing_fee_sats=args.max_xbt_routing_fee_sats)
        elif args.command == 'pay':
            answer = pay(directory)
        else:
            answer = result(private_load(directory/'customer.json'))
        print(json.dumps(answer))
        return 0
    except Exception:
        print(json.dumps(dict(event='customer_workflow_error', details='withheld',
                              automatic_resubmission=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
