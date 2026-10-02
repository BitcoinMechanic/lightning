#!/usr/bin/env python3
"""Read-only live BTC invoice, route and XBT channel inspection. Never pays."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from customer_errors import channel_reason
from market_check import minimum_sats
from neoxa_oracle import fetch
from reverse_oracle import estimate
from reverse_metadata import invoice_metadata
from reverse_policy import inspect_remote_policies
from reverse_timing import proposal as timing_proposal
from reverse_route import no_route, plan
from swap_rpc import RPC

READ_METHODS = {'getinfo', 'decode', 'getroutes', 'listpeerchannels', 'listfunds', 'listchannels'}


class CheckError(ValueError):
    def __init__(self, message, public_reason=None):
        super().__init__(message)
        self.public_reason = public_reason


class DiagnosticError(CheckError):
    def __init__(self, stage, error):
        super().__init__('read-only check failed; private details withheld')
        self.details = dict(stage=stage, error_type=type(error).__name__)
        if isinstance(error, subprocess.CalledProcessError):
            try:
                reply = json.loads(error.stdout)
                code = reply.get('code') if isinstance(reply, dict) else None
                if type(code) is int:
                    self.details['rpc_code'] = code
            except (ValueError, TypeError):
                pass
        # Only exact static messages from our validators may be displayed.
        safe = {
            'limit bid differs from ticker beyond reference gap limit',
            'stale or future ticker timestamp', 'insufficient limit-order bid depth',
            'bid proceeds fall below slippage limit', 'crossed or excessive market spread',
            'inconsistent ticker and order book', 'no limit-order bid liquidity',
            'oracle market mismatch', 'invalid liquidity type',
            'route planner returned too many hops or none',
            'route planner returned a discontinuous path',
            'route planner did not return a source-free route',
            'route planner channel direction mismatch',
            'reverse route exceeds fee budget', 'invalid reverse route amount or delay',
            'reverse route hop count outside bounds',
            'route planner changed requested amount or final CLTV',
        }
        if isinstance(error, ValueError) and str(error) in safe:
            self.details['validation'] = str(error)


@contextmanager
def diagnostic(stage):
    try:
        yield
    except CheckError:
        raise
    except Exception as error:
        raise DiagnosticError(stage, error) from None


def private_invoice(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 32768):
            raise CheckError('invoice file must be private, owned by this user and at most 32 KiB')
        invoice = stream.read(32769).strip()
    if (not invoice.lower().startswith('lnbc') or len(invoice) > 32768
            or any(c.isspace() for c in invoice)):
        raise CheckError('file must contain one BTC BOLT11 invoice')
    return invoice


@diagnostic('inspection')
def check(invoice, clis, *, rpc=RPC.call, market_fetch=fetch,
          now=time.time, monotonic=time.monotonic, max_routing_fee_sats=10,
          margin_bps=100, max_xbt_sats=500000, max_delay=288, _service_regtest=False, payer_id=None):
    if (type(max_routing_fee_sats) is not int or not 0 <= max_routing_fee_sats <= 100
            or type(margin_bps) is not int or not 0 <= margin_bps <= 500
            or type(max_xbt_sats) is not int or not 0 < max_xbt_sats <= 500000):
        raise CheckError('inspection caps: routing fee 0..100 sats, margin 0..500 bps, XBT 1..500000 sats')

    if type(max_delay) is not int or not 1 <= max_delay <= 2016:
        raise CheckError('inspection maximum delay must be 1..2016 blocks')

    def read(role, method, *args, named=False):
        if method not in READ_METHODS:
            raise CheckError('non-read RPC refused')
        with diagnostic('rpc.'+role+'.'+method):
            return rpc([*clis[role], *(['-k'] if named else [])], method, *args)

    if payer_id is not None and (not isinstance(payer_id, str) or not re.fullmatch('0[23][0-9a-f]{64}', payer_id)):
        raise CheckError('payer identity must be a compressed public key')
    infos = {}
    networks = ('regtest', 'xbt-regtest') if _service_regtest else ('bitcoin', 'xbt')
    for role, network in (('btc', networks[0]), ('operator', networks[1]), ('payer', networks[1])):
        if role == 'payer' and payer_id is not None:
            infos[role] = {'id': payer_id}
            continue
        info = read(role, 'getinfo')
        if info.get('network') != network or any(k.startswith('warning_') for k in info):
            raise CheckError('node network mismatch or node reports a warning')
        infos[role] = info
    if len({i['id'] for i in infos.values()}) != 3:
        raise CheckError('three distinct node identities required')
    decoded = read('btc', 'decode', invoice)
    amount = decoded.get('amount_msat')
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != ('bcrt' if _service_regtest else 'bc') or type(amount) is not int
            or not 1000 <= amount <= 10000000 or amount % 1000):
        raise CheckError('need signed BTC BOLT11 with a whole-sat amount from 1 to 10000 sats')
    for key in ('payment_hash', 'payment_secret'):
        if not isinstance(decoded.get(key), str) or not re.fullmatch('[0-9a-f]{64}', decoded[key]):
            raise CheckError('invoice lacks a supported payment hash or secret')
    if (not isinstance(decoded.get('payee'), str) or not re.fullmatch('0[23][0-9a-f]{64}', decoded['payee'])
            or decoded['payee'] == infos['btc']['id']):
        raise CheckError('unsupported or self-payment destination')
    try:
        metadata = invoice_metadata(decoded)
    except ValueError as error:
        raise CheckError(str(error)) from None
    for key in ('created_at', 'expiry', 'min_final_cltv_expiry'):
        if type(decoded.get(key)) is not int or decoded[key] < 0:
            raise CheckError('invalid invoice expiry or CLTV')
    if not 0 < decoded['min_final_cltv_expiry'] <= 144:
        raise CheckError('invoice final CLTV exceeds inspection limit of 144 blocks')
    expires = decoded['created_at'] + decoded['expiry']
    if expires - int(now()) < 120:
        raise CheckError('invoice expired or has less than two minutes remaining', 'invoice_expiring')

    def normal(c):
        return c.get('state') == 'CHANNELD_NORMAL' and c.get('peer_connected') is True and not c.get('htlcs')

    # Ignore historical closed channels; require one unambiguous current pair.
    with diagnostic('xbt_channels_and_reserves'):
        pair = {}
        for role, peer in (('payer', 'operator'), ('operator', 'payer')):
            if role == 'payer' and payer_id is not None:
                continue
            matches = [c for c in read(role, 'listpeerchannels')['channels']
                       if c.get('peer_id') == infos[peer]['id'] and c.get('state') == 'CHANNELD_NORMAL']
            readiness = channel_reason(matches, 'xbt')
            if readiness:
                raise CheckError('need one connected normal XBT channel without pending HTLCs at both ends', readiness)
            pair[role] = matches[0]
        spendable = None
        if payer_id is None:
            for key in ('channel_id', 'funding_txid', 'funding_outnum'):
                if key not in pair['payer'] or pair['payer'][key] != pair['operator'].get(key):
                    raise CheckError('XBT endpoints disagree on channel funding identity')
            spendable = pair['payer']['spendable_msat']
            if type(spendable) is not int or spendable < 0:
                raise CheckError('invalid XBT payer balance')
        receivable = pair['operator']['receivable_msat']
        if type(receivable) is not int or receivable < 0:
            raise CheckError('invalid XBT operator balance')
        xbt_min = max(minimum_sats(c) for c in pair.values())
        reserves = {}
        for role in ('btc', 'operator'):
            outputs = read(role, 'listfunds')['outputs']
            reserves[role] = sum(o['amount_msat'] for o in outputs
                                 if o['status'] == 'confirmed' and o.get('reserved') is False) >= 50000000
    start = monotonic()
    with diagnostic('market.ticker_fetch'):
        ticker = market_fetch('ticker')
    with diagnostic('market.orderbook_fetch'):
        book = market_fetch('orderbook')
    if monotonic()-start > 15:
        raise CheckError('oracle snapshot acquisition too slow')
    with diagnostic('market.validation'):
        audit = estimate(ticker, book, amount//1000, now_ms=int(now()*1000),
                         max_routing_fee_sats=max_routing_fee_sats, margin_bps=margin_bps)
    result = dict(read_only=True, live_payment_enabled=False, invoice_compatible=True,
                  payment_metadata_present=metadata is not None,
                  btc_sats=amount//1000, estimated_xbt_sats=audit['xbt_sats'],
                  max_routing_fee_sats=max_routing_fee_sats, margin_bps=margin_bps,
                  max_xbt_sats=max_xbt_sats, xbt_payer_spendable_sats=None if spendable is None else spendable//1000,
                  payer_rpc_checked=payer_id is None,
                  xbt_operator_receivable_sats=receivable//1000, xbt_minimum_sats=xbt_min,
                  operator_reserves_met=all(reserves.values()),
                  ticker_computed_at_ms=audit['ticker_computed_at_ms'],
                  route_found=False, feasible=False, inspection_max_delay=max_delay, remote_btc_htlc_minima_checked=False,
                  live_timing_policy_checked=False)

    def planner_rpc(cli, method, *args):
        if method != 'getroutes':
            raise CheckError('route inspection attempted a non-read RPC')
        return rpc(cli, method, *args)

    with diagnostic('btc.route_planning'):
        try:
            route, policy = plan(clis['btc'], decoded, infos['btc']['id'], planner_rpc,
                                 max_fee_msat=max_routing_fee_sats*1000, max_delay=max_delay,
                                 max_hops=8, _inspection=True, _service_regtest=_service_regtest)
        except subprocess.CalledProcessError as error:
            if not no_route(error):
                raise
            candidates = [c for c in read('btc', 'listpeerchannels')['channels']
                          if c.get('state') == 'CHANNELD_NORMAL']
            if candidates and all(c.get('peer_connected') is False for c in candidates):
                raise CheckError('all normal BTC peers are disconnected', 'btc_peer_disconnected')
            return dict(result, reason='no single-part BTC route within inspection limits')
    with diagnostic('btc.first_hop'):
        first = route[0]
        channels = [c for c in read('btc', 'listpeerchannels')['channels']
                    if c.get('peer_id') == first['id']
                    and first['channel'] in (c.get('short_channel_id'), c.get('alias', {}).get('local'))]
        if len(channels) != 1 or not normal(channels[0]):
            raise CheckError('planned BTC first-hop channel is not connected, normal and clear of HTLCs')
        btc = channels[0]
        if type(btc.get('spendable_msat')) is not int or btc['spendable_msat'] < 0:
            raise CheckError('invalid BTC first-hop capacity')
        btc_min = minimum_sats(btc)
        result.update(route_found=True, route_hops=len(route), routing_fee_msat=first['amount_msat']-amount,
                      btc_outgoing_cltv=first['delay'], invoice_final_cltv=decoded['min_final_cltv_expiry'],
                      btc_first_hop_spendable_sats=btc['spendable_msat']//1000,
                      btc_first_hop_minimum_sats=btc_min)
    result['timing_proposal'] = timing_proposal(first['delay'])
    with diagnostic('btc.remote_policies'):
        policy_audit = inspect_remote_policies(
            route, lambda scid: read('btc', 'listchannels', scid))
    result.update(policy_audit)
    reasons = list(policy_audit['remote_btc_policy_violations'])
    if not result['timing_proposal']['fits_default_cltv_budget']:
        reasons.append('route exceeds proposed cross-chain timing budget')
    if policy_audit['remote_btc_policy_hops_unknown']:
        reasons.append('remote HTLC limits unavailable for one or more planned hops')
    if not all(reserves.values()):
        reasons.append('operator confirmed unreserved reserve below 50000 sats')
    if audit['xbt_sats'] > max_xbt_sats:
        reasons.append('estimated XBT exceeds inspection cap')
    if audit['xbt_sats']*1000 > (receivable if spendable is None else min(spendable, receivable)):
        reasons.append('insufficient XBT payer-to-operator liquidity')
    if audit['xbt_sats'] < xbt_min:
        reasons.append('XBT amount below conservative untrimmed minimum')
    if first['amount_msat'] > btc['spendable_msat']:
        reasons.append('insufficient BTC first-hop liquidity including routing fee')
    if first['amount_msat'] < btc_min*1000:
        reasons.append('BTC first-hop amount below conservative untrimmed minimum')
    checked = int(now())
    if expires-checked < 120 or checked*1000-audit['ticker_computed_at_ms'] > 30000:
        raise CheckError('invoice or price became stale during route inspection; fetch anew')
    result.update(checked_at=checked, invoice_seconds_remaining=expires-checked,
                  feasible=not reasons, reasons=reasons)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invoice-file', required=True, type=Path)
    parser.add_argument('--btc-dir', type=Path, default=Path.home()/'cln-btc-observe')
    parser.add_argument('--xbt-operator-dir', type=Path, default=Path.home()/'cln-xbt-observe')
    parser.add_argument('--xbt-payer-dir', type=Path, default=Path.home()/'cln-xbt-peer')
    parser.add_argument('--max-routing-fee-sats', type=int, default=10)
    parser.add_argument('--max-delay', type=int, default=288,
                        help='read-only route delay cap in blocks (1..2016; default 288)')
    parser.add_argument('--margin-bps', type=int, default=100)
    parser.add_argument('--max-xbt-sats', type=int, default=500000)
    parser.add_argument('--operator-only', action='store_true', help='Use the bound payer identity in service settings without contacting its wallet.')
    parser.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    args = parser.parse_args()
    binary = str(Path(__file__).resolve().parents[2]/'cli/lightning-cli')
    clis = {role: [binary, '--lightning-dir='+str(directory.expanduser().resolve()),
                   '--network='+network, '--json', '--notifications=none']
            for role, directory, network in (('btc', args.btc_dir, 'bitcoin'),
                                             ('operator', args.xbt_operator_dir, 'xbt'),
                                             ('payer', args.xbt_payer_dir, 'xbt'))}
    try:
        payer_id = None
        if args.operator_only:
            from service_manager import private_load
            settings = private_load(args.settings.expanduser())
            payer_id = settings['receiver_id']
            clis = dict(btc=settings['btc_cli'], operator=settings['xbt_cli'])
        print(json.dumps(check(private_invoice(args.invoice_file.expanduser()), clis,
                               max_routing_fee_sats=args.max_routing_fee_sats,
                               margin_bps=args.margin_bps, max_xbt_sats=args.max_xbt_sats,
                               max_delay=args.max_delay, payer_id=payer_id)))
        return 0
    except DiagnosticError as error:
        print(json.dumps(dict(event='inspection_failed', reason=str(error), **error.details)))
        return 1
    except Exception as error:
        print(json.dumps(dict(event='inspection_failed', reason=str(error) if isinstance(error, CheckError)
                              else 'read-only RPC or market validation failed; private details withheld')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
