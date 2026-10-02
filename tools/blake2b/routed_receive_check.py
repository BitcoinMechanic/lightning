#!/usr/bin/env python3
"""Private-safe, read-only live BTC -> routed XBT inspection. Never quotes or pays."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time

from market_check import minimum_sats
from neoxa_oracle import estimate, fetch
from reverse_check import CheckError, DiagnosticError, diagnostic, private_invoice
from reverse_metadata import invoice_metadata
from reverse_policy import inspect_remote_policies
from reverse_route import plan_with_hints, no_route
from service_manager import private_load
from swap_rpc import RPC
from receive_bounds import timing as candidate_timing

READ_METHODS = {'getinfo', 'decode', 'getroutes', 'listpeerchannels', 'listfunds', 'listchannels'}


def timing(delay):
    try:
        return candidate_timing(delay)
    except ValueError as error:
        raise CheckError(str(error)) from None


def normal(channel):
    return (channel.get('state') == 'CHANNELD_NORMAL' and channel.get('peer_connected') is True
            and not channel.get('htlcs'))


def balance(channel, key):
    value = channel.get(key)
    if type(value) is not int or value < 0:
        raise CheckError('invalid channel balance')
    return value


@diagnostic('inspection')
def check(invoice, settings, *, rpc=RPC.call, market_fetch=fetch, now=time.time,
          monotonic=time.monotonic, max_xbt_sats=500000, max_btc_sats=10000,
          max_xbt_routing_fee_sats=10, margin_bps=100, max_delay=288):
    for value, low, high in ((max_xbt_sats,1,500000),(max_btc_sats,1,10000),
                             (max_xbt_routing_fee_sats,0,100),(margin_bps,0,500),(max_delay,1,2016)):
        if type(value) is not int or not low <= value <= high:
            raise CheckError('inspection cap outside supported bounds')
    ids = settings.get('node_ids')
    if (not isinstance(ids,list) or len(ids)!=2 or ids[0]==ids[1]
            or any(not isinstance(n,str) or not re.fullmatch('0[23][0-9a-f]{64}',n) for n in ids)):
        raise CheckError('settings need two distinct operator identities')
    clis = {role: settings[role+'_cli'] for role in ('btc','xbt')}
    for cli in clis.values():
        if not isinstance(cli,list) or not cli or any(not isinstance(s,str) or not s for s in cli):
            raise CheckError('invalid operator RPC command')
    def read(role, method, *args, named=False):
        if method not in READ_METHODS:
            raise CheckError('non-read RPC refused')
        with diagnostic('rpc.'+role+'.'+method):
            return rpc([*clis[role], *(['-k'] if named else [])],method,*args)
    for role,network,expected in zip(('btc','xbt'),('bitcoin','xbt'),ids):
        info=read(role,'getinfo')
        if (info.get('network')!=network or info.get('id')!=expected
                or any(k.startswith('warning_') for k in info)):
            raise CheckError('operator network, identity or readiness mismatch')
    decoded=read('xbt','decode',invoice)
    amount=decoded.get('amount_msat')
    if (decoded.get('valid') is not True or decoded.get('type')!='bolt11 invoice'
            or decoded.get('currency')!='xbt' or type(amount) is not int
            or not 1000<=amount<=500000000 or amount%1000):
        raise CheckError('need a signed whole-sat XBT invoice from 1 to 500000 sats')
    for key in ('payment_hash','payment_secret'):
        if not isinstance(decoded.get(key),str) or not re.fullmatch('[0-9a-f]{64}',decoded[key]):
            raise CheckError('unsupported payment hash or secret')
    if (not isinstance(decoded.get('payee'),str) or not re.fullmatch('0[23][0-9a-f]{64}',decoded['payee'])
            or decoded['payee'] in ids):
        raise CheckError('unsupported or self-payment destination')
    with diagnostic('xbt.invoice_features'):
        if invoice_metadata(decoded) is not None:
            raise CheckError('routed XBT execution does not yet support payment metadata')
    for key in ('created_at','expiry','min_final_cltv_expiry'):
        if type(decoded.get(key)) is not int or decoded[key]<0:
            raise CheckError('invalid invoice expiry or CLTV')
    if not 0<decoded['min_final_cltv_expiry']<=144:
        raise CheckError('XBT invoice final CLTV exceeds inspection bounds')
    expires=decoded['created_at']+decoded['expiry']
    if expires-int(now())<120:
        raise CheckError('XBT invoice needs at least two minutes remaining')
    started=monotonic()
    with diagnostic('market.ticker_fetch'): ticker=market_fetch('ticker')
    with diagnostic('market.orderbook_fetch'): book=market_fetch('orderbook')
    if monotonic()-started>15:
        raise CheckError('oracle snapshot acquisition too slow')
    budget=amount//1000+max_xbt_routing_fee_sats
    with diagnostic('market.validation'):
        audit=estimate(ticker,book,budget,now_ms=int(now()*1000),margin_bps=margin_bps)
    reasons=[]
    if budget>max_xbt_sats: reasons.append('XBT amount including fee allowance exceeds cap')
    if audit['btc_sats']>max_btc_sats: reasons.append('estimated BTC exceeds cap')
    reserves=[]
    with diagnostic('operator.reserves'):
        for role in ('btc','xbt'):
            outputs=read(role,'listfunds')['outputs']
            values=[o['amount_msat'] for o in outputs if o['status']=='confirmed' and o.get('reserved') is False]
            if any(type(v) is not int or v<0 for v in values): raise CheckError('invalid reserve amount')
            reserves.append(sum(values)>=50000000)
    if not all(reserves): reasons.append('operator confirmed unreserved reserve below 50000 sats')
    with diagnostic('btc.incoming_capacity'):
        candidates=[c for c in read('btc','listpeerchannels')['channels'] if normal(c) and c.get('short_channel_id')]
        eligible=[c for c in candidates if balance(c,'receivable_msat')>=audit['btc_sats']*1000
                  and audit['btc_sats']>=minimum_sats(c)]
    if not eligible: reasons.append('no clear connected BTC channel can accept the quoted amount untrimmed')
    result=dict(read_only=True,live_payment_enabled=False,invoice_compatible=True,customer_rpc_checked=False,
                receiver_xbt_sats=amount//1000,xbt_budget_sats=budget,estimated_btc_sats=audit['btc_sats'],
                max_xbt_routing_fee_sats=max_xbt_routing_fee_sats,margin_bps=margin_bps,
                max_xbt_sats=max_xbt_sats,max_btc_sats=max_btc_sats,
                routing_fee_allowance_included=True,operator_reserves_met=all(reserves),
                eligible_btc_incoming_channels=len(eligible),route_found=False,feasible=False,
                inspection_max_delay=max_delay,live_timing_policy_checked=False,
                remote_xbt_htlc_minima_checked=False,ticker_computed_at_ms=audit['ticker_computed_at_ms'])
    def planner_rpc(cli,method,*args):
        if cli != [*clis['xbt'],'-k'] or method!='getroutes':
            raise CheckError('unexpected route inspection RPC')
        # Preserve RPC error codes for bounded hint fallback; the outer
        # route-planning diagnostic redacts failures that actually escape.
        return rpc(cli,method,*args)
    policy=dict(source=ids[1],destination=decoded['payee'],max_fee_msat=max_xbt_routing_fee_sats*1000,
                max_delay=max_delay,max_hops=8,final_cltv=max(40,decoded['min_final_cltv_expiry']))
    with diagnostic('xbt.route_planning'):
        try:
            route,policy=plan_with_hints(clis['xbt'],amount,policy,decoded.get('routes',[]),planner_rpc,
                                        _inspection=True,_xbt_inspection=True)
        except subprocess.CalledProcessError as error:
            if not no_route(error): raise
            reasons.append('no single-part XBT route within inspection limits')
            result['reasons']=reasons
            return result
    first=route[0]
    with diagnostic('xbt.first_hop'):
        channels=[c for c in read('xbt','listpeerchannels')['channels'] if c.get('peer_id')==first['id']
                  and first['channel'] in (c.get('short_channel_id'),c.get('alias',{}).get('local'))]
        if len(channels)!=1 or not normal(channels[0]):
            raise CheckError('planned XBT first hop is not connected, normal and clear of HTLCs')
        c=channels[0];spendable=balance(c,'spendable_msat');minimum=minimum_sats(c)
        if spendable<first['amount_msat']: reasons.append('insufficient XBT first-hop liquidity including routing fee')
        if first['amount_msat']<minimum*1000: reasons.append('XBT first-hop HTLC below conservative untrimmed minimum')
    with diagnostic('xbt.remote_policies'):
        remote=inspect_remote_policies(route,lambda scid:read('xbt','listchannels',scid))
    remote={k.replace('remote_btc_','remote_xbt_'):v for k,v in remote.items()}
    reasons.extend(remote['remote_xbt_policy_violations'])
    if remote['remote_xbt_policy_hops_unknown']:
        reasons.append('remote XBT HTLC limits unavailable for one or more planned hops')
    candidate=timing(first['delay'])
    if not candidate['fits_default_cltv_budget']: reasons.append('route exceeds proposed BTC timing budget')
    checked=int(now())
    if expires-checked<120 or not 0<=checked*1000-audit['ticker_computed_at_ms']<=30000:
        raise CheckError('invoice or market snapshot became stale during inspection')
    result.update(remote,route_found=True,route_hops=len(route),routing_fee_msat=first['amount_msat']-amount,
                  xbt_outgoing_cltv=first['delay'],invoice_final_cltv=decoded['min_final_cltv_expiry'],
                  xbt_first_hop_spendable_sats=spendable//1000,xbt_first_hop_minimum_sats=minimum,
                  timing_proposal=candidate,checked_at=checked,invoice_seconds_remaining=expires-checked,
                  feasible=not reasons,reasons=reasons)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--invoice-file',type=Path,required=True)
    p.add_argument('--settings',type=Path,default=Path.home()/'.config/cln-swaps/settings.json')
    p.add_argument('--max-xbt-sats',type=int,default=500000)
    p.add_argument('--max-btc-sats',type=int,default=10000)
    p.add_argument('--max-xbt-routing-fee-sats',type=int,default=10)
    p.add_argument('--margin-bps',type=int,default=100)
    p.add_argument('--max-delay',type=int,default=288)
    a=p.parse_args(argv)
    try:
        print(json.dumps(check(private_invoice(a.invoice_file.expanduser(),currency='xbt'),
                               private_load(a.settings.expanduser()),max_xbt_sats=a.max_xbt_sats,
                               max_btc_sats=a.max_btc_sats,max_xbt_routing_fee_sats=a.max_xbt_routing_fee_sats,
                               margin_bps=a.margin_bps,max_delay=a.max_delay)))
        return 0
    except DiagnosticError as error:
        print(json.dumps(dict(event='inspection_failed',reason=str(error),**error.details)))
    except Exception as error:
        print(json.dumps(dict(event='inspection_failed',reason=str(error) if isinstance(error,CheckError)
                              else 'read-only inspection failed; private details withheld')))
    return 1


if __name__=='__main__':raise SystemExit(main())
