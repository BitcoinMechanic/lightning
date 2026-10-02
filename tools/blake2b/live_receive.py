"""Explicit bounded live routed receiving. No installer or implicit activation.

The selected single-part route is immutable. Quotes price the full outgoing
fee allowance; submission and recovery never consult the oracle or replan.
"""
import copy
import hashlib
import json
import re
import secrets
import time

import incoming_btc
import live_pilot as pilot
import neoxa_oracle as oracle
import receive_bounds as bounds
from reverse_metadata import invoice_metadata
from reverse_route import plan_with_hints, validate as validate_route
from service_manager import private_load
from swap_invoice import unsigned_invoice
from swap_rpc import RPC

PROFILE = 'live-routed-receive-v1'
MODE = 'bounded-xbt-live-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def hex32(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def node_id(value):
    return isinstance(value,str) and re.fullmatch('0[23][0-9a-f]{64}',value) is not None


def validate_config(config):
    if (set(config) != {'profile','btc_cli','xbt_cli','node_ids','market',
                       'max_xbt_routing_fee_msat','max_delay'} or config['profile'] != PROFILE):
        raise ValueError('unsupported live routed receive configuration')
    for key in ('btc_cli','xbt_cli'):
        if not isinstance(config[key],list) or not config[key] or any(not isinstance(v,str) or not v for v in config[key]):
            raise ValueError('invalid operator RPC command')
    ids=config['node_ids']
    if not isinstance(ids,list) or len(ids)!=2 or ids[0]==ids[1] or not all(map(node_id,ids)):
        raise ValueError('need two pinned distinct operator identities')
    market=config['market']
    if not isinstance(market,dict) or set(market)!={'max_btc_sats','max_xbt_sats','margin_bps'}:
        raise ValueError('unexpected routed market fields')
    for value,lo,hi in ((market['max_btc_sats'],1,10000),(market['max_xbt_sats'],1,500000),
                        (market['margin_bps'],0,500),(config['max_xbt_routing_fee_msat'],0,100000),
                        (config['max_delay'],40,1842)):
        if type(value) is not int or not lo<=value<=hi:
            raise ValueError('live routed cap outside supported bounds')


def configuration(settings):
    config=copy.deepcopy(settings['receive_policy'])
    validate_config(config)
    if any(config[k]!=settings[k] for k in ('btc_cli','xbt_cli','node_ids')):
        raise ValueError('routed policy differs from operator settings')
    return config


def identities(config,rpc=None):
    rpc=rpc or RPC.call
    validate_config(config)
    for key,network,expected in zip(('btc_cli','xbt_cli'),('bitcoin','xbt'),config['node_ids']):
        info=rpc(config[key],'getinfo')
        if info.get('network')!=network or info.get('id')!=expected:
            raise ValueError('live routed operator identity or network changed')
    return config['node_ids']


def gate_ready(config,rpc):
    if rpc(config['btc_cli'],'xbt-pilot-info').get('profile')!=PROFILE:
        raise ValueError('live routed gate is not explicitly enabled')


def invoice(decoded,config):
    amount=decoded.get('amount_msat')
    if (decoded.get('valid') is not True or decoded.get('type')!='bolt11 invoice'
            or decoded.get('currency')!='xbt' or type(amount) is not int
            or amount%1000 or not 1000<=amount<=config['market']['max_xbt_sats']*1000
            or not hex32(decoded.get('payment_hash')) or not hex32(decoded.get('payment_secret'))
            or not node_id(decoded.get('payee')) or decoded['payee'] in config['node_ids']
            or type(decoded.get('min_final_cltv_expiry')) is not int
            or not 1<=decoded['min_final_cltv_expiry']<=144
            or invoice_metadata(decoded) is not None):
        raise ValueError('unsupported live routed XBT invoice')
    for k in ('created_at','expiry'):
        if type(decoded.get(k)) is not int or decoded[k]<0:
            raise ValueError('invalid invoice expiry')
    return amount


def validate_state(state):
    config=state['routed_config'];validate_config(config)
    if (state.get('profile')!=PROFILE or state.get('xbt_routing')!=MODE
            or state.get('quote_gate') is not True or state.get('btc_deadline_guard') is not True
            or type(state.get('btc_close_blocks')) is not int or state['btc_close_blocks']!=72
            or state.get('btc_channel_policy')!=incoming_btc.POLICY or 'btc_channel' in state
            or any(state.get(k)!=config[k] for k in ('btc_cli','xbt_cli','node_ids'))
            or state.get('btc_node_id')!=config['node_ids'][0]
            or not hex32(state.get('payment_hash')) or not hex32(state.get('payment_secret'))
            or not hex32(state.get('controller_id'))):
        raise ValueError('live routed state policy differs')
    btc,xbt=state['btc_amount_msat'],state['xbt_amount_msat']
    for value,cap in ((btc,config['market']['max_btc_sats']),(xbt,config['market']['max_xbt_sats'])):
        if type(value) is not int or value%1000 or not 0<value<=cap*1000:
            raise ValueError('live routed amount outside operator caps')
    policy=state['xbt_route_policy']
    if (policy.get('source')!=config['node_ids'][1] or not node_id(policy.get('destination'))
            or policy['destination'] in config['node_ids']
            or policy.get('max_fee_msat')!=config['max_xbt_routing_fee_msat']
            or policy.get('max_delay')!=config['max_delay'] or policy.get('max_hops')!=8
            or not 40<=policy.get('final_cltv',0)<=144):
        raise ValueError('live routed route policy differs')
    # These flags select the established numeric XBT inspection bounds in a
    # pure validator. Execution permission comes from this explicit profile.
    validate_route(state['route'],xbt,policy,_inspection=True,_xbt_inspection=True)
    if not all(node_id(h['id']) for h in state['route']):
        raise ValueError('invalid routed peer identity')
    pin=state['xbt_first_hop'];first=state['route'][0]
    if (set(pin)!={'short_channel_id','peer_id','channel_id','funding_txid','funding_outnum'}
            or pin['short_channel_id']!=first['channel'] or pin['peer_id']!=first['id']
            or not hex32(pin['channel_id']) or not hex32(pin['funding_txid'])
            or type(pin['funding_outnum']) is not int or not 0<=pin['funding_outnum']<=65535):
        raise ValueError('live routed funding pin differs')
    timing=bounds.timing(first['delay'])
    if state.get('xbt_timing')!=timing or not timing['fits_default_cltv_budget']:
        raise ValueError('live routed timing changed')
    audit=state['oracle'];budget=(xbt+config['max_xbt_routing_fee_msat']+999)//1000
    if (budget>config['market']['max_xbt_sats'] or audit.get('xbt_sats')!=budget
            or audit.get('receiver_xbt_sats')!=xbt//1000 or audit.get('btc_sats')!=btc//1000
            or audit.get('margin_bps')!=config['market']['margin_bps']
            or audit.get('max_xbt_routing_fee_msat')!=config['max_xbt_routing_fee_msat']
            or audit.get('routing_fee_allowance_included') is not True
            or digest(audit)!=state.get('oracle_digest')
            or type(state.get('quote_expires_at')) is not int):
        raise ValueError('live routed oracle or amount binding differs')
    return btc,xbt


def verify_state(state,rpc):
    validate_state(state)
    identities(state['routed_config'],rpc)


def check_gate(state,terms):
    validate_state(state)
    expected=dict(pilot=PROFILE,payment_hash=state['payment_hash'],
                  btc_amount_msat=state['btc_amount_msat'],xbt_amount_msat=state['xbt_amount_msat'],
                  btc_channel_policy=incoming_btc.POLICY,xbt_invoice=state['xbt_invoice'],
                  expires_at=state['quote_expires_at'],oracle_digest=state['oracle_digest'],
                  controller_id=state['controller_id'],xbt_route_delay=state['route'][0]['delay'],
                  xbt_route_digest=bounds.route_digest(state),
                  min_cltv_delta=state['xbt_timing']['minimum_btc_remaining_blocks'],max_cltv_delta=2016)
    if any(terms.get(k)!=v for k,v in expected.items()) or terms.get('btc_channel') is not None:
        raise ValueError('held live routed quote differs from original intent')


def first_hop(state,rpc):
    first=state['route'][0]
    rows=[c for c in rpc(state['xbt_cli'],'listpeerchannels')['channels']
          if c.get('short_channel_id')==first['channel'] and c.get('peer_id')==first['id']]
    if (len(rows)!=1 or rows[0].get('state')!='CHANNELD_NORMAL'
            or rows[0].get('peer_connected') is not True or rows[0].get('htlcs')
            or rows[0].get('spendable_msat',0)<first['amount_msat']):
        raise ValueError('live routed first hop lacks clear connected liquidity')
    return rows[0]


def pre_spend(state,decoded,remaining,rpc):
    verify_state(state,rpc)
    config=state['routed_config'];invoice(decoded,config)
    if (any(decoded.get(k)!=state[v] for k,v in (('payment_hash','payment_hash'),
           ('payment_secret','payment_secret'),('amount_msat','xbt_amount_msat')))
            or decoded['payee']!=state['xbt_route_policy']['destination']
            or max(40,decoded['min_final_cltv_expiry'])!=state['xbt_route_policy']['final_cltv']
            or decoded['created_at']+decoded['expiry']<=int(time.time())
            or state['quote_expires_at']<=int(time.time())
            or type(remaining) is not int
            or not state['xbt_timing']['minimum_btc_remaining_blocks']<=remaining<=2016):
        raise ValueError('live routed invoice or fresh timelock differs')
    if any(p.get("payment_hash")==state["payment_hash"]
           for p in rpc(state["xbt_cli"],"listsendpays")["payments"]):
        raise ValueError("XBT invoice already has an outgoing attempt")
    gate_ready(config,rpc)
    pilot.require_reserves(state,rpc)
    channel=first_hop(state,rpc)
    if any(channel.get(k)!=v for k,v in state['xbt_first_hop'].items()):
        raise ValueError('live outgoing funding output changed')
    pilot.require_untrimmed(channel,state['route'][0]['amount_msat'])
    from reverse_policy import inspect_remote_policies
    audit=inspect_remote_policies(state['route'],lambda scid:rpc(state['xbt_cli'],'listchannels',scid))
    if not audit['remote_btc_htlc_limits_passed']:
        raise ValueError('remote XBT limits unavailable or changed')


def validate_quote(data):
    config=data['config'];state=data['controller'];terms=data['terms']
    validate_config(config);validate_state(state);check_gate(state,terms)
    if (state['routed_config']!=config or data['node_ids']!=state['node_ids']
            or data['oracle']!=state['oracle'] or state['phase']!='prepared'
            or not hex32(terms.get('payment_secret'))):
        raise ValueError('live routed quote binding differs')


def preflight(data,rpc=None):
    rpc=rpc or RPC.call;validate_quote(data)
    state=data['controller']
    decoded=rpc(state['xbt_cli'],'decode',state['xbt_invoice'])
    if decoded['created_at']+decoded['expiry']<state['quote_expires_at']+60:
        raise ValueError('quoted invoice expiry changed')
    pre_spend(state,decoded,state['xbt_timing']['minimum_btc_remaining_blocks'],rpc)


def create(config,bolt11,btc_sats,directory):
    from swap_controller import save
    if btc_sats is not None:raise ValueError('routed quote requires oracle pricing')
    identities(config,RPC.call);gate_ready(config,RPC.call)
    decoded=RPC.call(config['xbt_cli'],'decode',bolt11);amount=invoice(decoded,config)
    now=int(time.time());expires=min(now+120,decoded['created_at']+decoded['expiry']-60)
    if expires<now+30:raise ValueError('XBT invoice needs at least 90 seconds remaining')
    if any(p['payment_hash']==decoded['payment_hash'] for p in RPC.call(config['xbt_cli'],'listsendpays')['payments']):
        raise ValueError('XBT invoice already has an outgoing attempt')
    policy=dict(source=config['node_ids'][1],destination=decoded['payee'],max_hops=8,
                max_fee_msat=config['max_xbt_routing_fee_msat'],max_delay=config['max_delay'],
                final_cltv=max(40,decoded['min_final_cltv_expiry']))
    route,policy=plan_with_hints(config['xbt_cli'],amount,policy,decoded.get('routes',[]),RPC.call,
                                 _inspection=True,_xbt_inspection=True)
    budget=(amount+config['max_xbt_routing_fee_msat']+999)//1000
    if budget>config['market']['max_xbt_sats']:raise ValueError('XBT amount including fee allowance exceeds cap')
    start=time.monotonic();ticker,book=oracle.fetch('ticker'),oracle.fetch('orderbook')
    if time.monotonic()-start>15:raise ValueError('market snapshot acquisition too slow')
    audit=oracle.estimate(ticker,book,budget,now_ms=int(time.time()*1000),margin_bps=config['market']['margin_bps'])
    if audit['btc_sats']>config['market']['max_btc_sats']:raise ValueError('oracle BTC amount exceeds operator cap')
    audit.update(receiver_xbt_sats=amount//1000,max_xbt_routing_fee_msat=config['max_xbt_routing_fee_msat'],
                 routing_fee_allowance_included=True,lightning_fees_included=True)
    state=dict(profile=PROFILE,phase='prepared',quote_gate=True,btc_deadline_guard=True,btc_close_blocks=72,
               btc_cli=config['btc_cli'],xbt_cli=config['xbt_cli'],node_ids=config['node_ids'],
               btc_node_id=config['node_ids'][0],btc_channel_policy=incoming_btc.POLICY,
               xbt_routing=MODE,xbt_route_policy=policy,route=route,xbt_timing=bounds.timing(route[0]['delay']),
               payment_hash=decoded['payment_hash'],payment_secret=decoded['payment_secret'],xbt_invoice=bolt11,
               xbt_amount_msat=amount,btc_amount_msat=audit['btc_sats']*1000,quote_expires_at=expires,
               routed_config=copy.deepcopy(config),oracle=audit,oracle_digest=digest(audit),controller_id=secrets.token_hex(32))
    channel=first_hop(state,RPC.call)
    state['xbt_first_hop']={k:channel[k] for k in ('short_channel_id','peer_id','channel_id','funding_txid','funding_outnum')}
    terms=dict(pilot=PROFILE,payment_hash=state['payment_hash'],payment_secret=secrets.token_hex(32),
               btc_amount_msat=state['btc_amount_msat'],xbt_amount_msat=amount,xbt_invoice=bolt11,expires_at=expires,
               min_cltv_delta=state['xbt_timing']['minimum_btc_remaining_blocks'],max_cltv_delta=2016,
               btc_channel_policy=incoming_btc.POLICY,oracle_digest=state['oracle_digest'],controller_id=state['controller_id'],
               xbt_route_delay=route[0]['delay'],xbt_route_digest=bounds.route_digest(state))
    data=dict(config=copy.deepcopy(config),node_ids=config['node_ids'],terms=terms,controller=state,oracle=audit)
    preflight(data);incoming_btc.preflight(config,terms['btc_amount_msat'],RPC.call)
    if not 0<=int(time.time()*1000)-audit['ticker_computed_at_ms']<=30000:
        raise ValueError('market snapshot became stale during quote preparation')
    directory.mkdir(mode=0o700,parents=True,exist_ok=False);save(directory/'quote.json',data)


def publish(directory):
    from swap_controller import save
    path=directory/'quote.json';data=private_load(path);preflight(data)
    if 'btc_invoice' in data:return
    terms=data['terms'];cli=data['config']['btc_cli'];final=data['controller']['xbt_timing']['proposed_btc_invoice_cltv']
    incoming_btc.preflight(data['config'],terms['btc_amount_msat'],RPC.call)
    if RPC.call(cli,'xbt-register',json.dumps(terms))!={'registered':True}:raise RuntimeError('quote registration failed')
    unsigned=unsigned_invoice(terms['payment_hash'],terms['payment_secret'],terms['btc_amount_msat'],
                              terms['expires_at']-int(time.time()),currency='bc',final_cltv=final)
    signed=RPC.call(cli,'signinvoice',unsigned)['bolt11'];decoded=RPC.call(cli,'decode',signed)
    expected=dict(valid=True,currency='bc',payee=data['node_ids'][0],payment_hash=terms['payment_hash'],
                  payment_secret=terms['payment_secret'],amount_msat=terms['btc_amount_msat'],min_final_cltv_expiry=final)
    if any(decoded.get(k)!=v for k,v in expected.items()):raise RuntimeError('signed BTC invoice differs from routed quote')
    data['btc_invoice']=signed;save(path,data)
