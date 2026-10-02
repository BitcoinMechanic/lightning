"""Public-route receiving API backend. Explicit regtest-only policy.

The market estimate prices the receiver amount plus the entire fee allowance.
A quote pins one route and funding output; the worker never replans or reprices.
"""
import copy
import json
import secrets
import time

import incoming_btc
import neoxa_oracle as oracle
import outgoing_xbt as routed
from service_manager import private_load
from swap_controller import save
from swap_invoice import unsigned_invoice
from swap_rpc import RPC

PROFILE = 'routed-receive-regtest-v1'
MIN_CLTV = 140
INVOICE_CLTV = 160


def is_config(config):
    return config.get('profile') == PROFILE


def validate_config(config):
    if (set(config) != {'profile', 'btc_cli', 'xbt_cli', 'market', 'max_xbt_routing_fee_msat'}
            or not is_config(config)):
        raise ValueError('unsupported routed receiving configuration')
    for key in ('btc_cli', 'xbt_cli'):
        if (not isinstance(config[key], list) or not config[key]
                or any(not isinstance(x, str) or not x for x in config[key])):
            raise ValueError('invalid operator RPC command')
    market = config['market']
    if set(market) != {'max_btc_sats', 'max_xbt_sats', 'margin_bps'}:
        raise ValueError('unexpected routed market policy')
    for value, low, high in ((market['max_btc_sats'], 1, 10000),
                             (market['max_xbt_sats'], 1, 500000),
                             (market['margin_bps'], 0, 500),
                             (config['max_xbt_routing_fee_msat'], 0, 10000)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('routed receiving cap outside fixture bounds')


def configuration(settings):
    config = copy.deepcopy(settings['receive_policy'])
    validate_config(config)
    if any(config[k] != settings[k] for k in ('btc_cli', 'xbt_cli')):
        raise ValueError('routed policy differs from operator commands')
    return config


def identities(config):
    validate_config(config)
    ids = []
    for role, network in (('btc_cli', 'regtest'), ('xbt_cli', 'xbt-regtest')):
        info = RPC.call(config[role], 'getinfo')
        if info['network'] != network or any(k.startswith('warning_') for k in info):
            raise ValueError('routed receiving requires ready regtest operators')
        ids.append(info['id'])
    if ids[0] == ids[1]:
        raise ValueError('operator identities must differ')
    return ids


def pricing(config, ticker, book, now_ms):
    validate_config(config)
    # Round up only once, after adding the millisatoshi routing allowance.
    budget = (routed.AMOUNT + config['max_xbt_routing_fee_msat'] + 999) // 1000
    if budget > config['market']['max_xbt_sats']:
        raise ValueError('XBT amount including routing allowance exceeds operator cap')
    audit = oracle.estimate(ticker, book, budget, now_ms=now_ms,
                            margin_bps=config['market']['margin_bps'])
    if audit['btc_sats'] > config['market']['max_btc_sats']:
        raise ValueError('oracle BTC amount exceeds operator cap')
    audit.update(receiver_xbt_sats=routed.AMOUNT // 1000,
                 max_xbt_routing_fee_msat=config['max_xbt_routing_fee_msat'],
                 routing_fee_allowance_included=True, lightning_fees_included=True)
    return audit


def validate_quote(data):
    """Structural binding checks; authorization separately pins the whole quote."""
    config, terms, state, audit = (data[k] for k in ('config', 'terms', 'controller', 'oracle'))
    validate_config(config)
    budget = (routed.AMOUNT + config['max_xbt_routing_fee_msat'] + 999) // 1000
    expected = dict(profile='regtest', phase='prepared', quote_gate=True,
                    btc_cli=config['btc_cli'], xbt_cli=config['xbt_cli'], node_ids=data['node_ids'],
                    btc_node_id=data['node_ids'][0], xbt_routing=routed.MODE,
                    btc_channel_policy=incoming_btc.POLICY,
                    payment_hash=terms['payment_hash'], xbt_invoice=terms['xbt_invoice'],
                    xbt_amount_msat=routed.AMOUNT, btc_amount_msat=terms['btc_amount_msat'])
    if (any(state.get(k) != v for k, v in expected.items())
            or terms['xbt_amount_msat'] != routed.AMOUNT
            or terms.get('btc_channel_policy') != incoming_btc.POLICY
            or 'btc_channel' in terms or 'btc_channel' in state
            or terms['min_cltv_delta'] != MIN_CLTV or terms['max_cltv_delta'] != 2000
            or type(audit['btc_sats']) is not int or not 0 < audit['btc_sats'] <= config['market']['max_btc_sats']
            or terms['btc_amount_msat'] != audit['btc_sats'] * 1000
            or audit.get('xbt_sats') != budget or budget > config['market']['max_xbt_sats']
            or audit.get('receiver_xbt_sats') != routed.AMOUNT // 1000
            or audit.get('margin_bps') != config['market']['margin_bps']
            or audit.get('max_xbt_routing_fee_msat') != config['max_xbt_routing_fee_msat']
            or audit.get('routing_fee_allowance_included') is not True):
        raise ValueError('routed quote amount, pricing or controller binding differs')
    policy = state['xbt_route_policy']
    if (policy['source'] != data['node_ids'][1]
            or policy['max_fee_msat'] != config['max_xbt_routing_fee_msat']
            or policy['max_delay'] != 80 or policy['max_hops'] != 4 or policy['final_cltv'] != 40):
        raise ValueError('routed quote route policy differs')
    routed.validate(state['route'], routed.AMOUNT, policy)
    pin = state['xbt_first_hop']
    if pin['short_channel_id'] != state['route'][0]['channel'] or pin['peer_id'] != state['route'][0]['id']:
        raise ValueError('routed quote funding pin differs')


def preflight(data, rpc=None):
    rpc = rpc or RPC.call
    validate_quote(data)
    state, terms = data['controller'], data['terms']
    routed.verify(state, rpc)
    decoded = rpc(state['xbt_cli'], 'decode', state['xbt_invoice'])
    if (decoded.get('payment_hash') != state['payment_hash']
            or decoded.get('payment_secret') != state['payment_secret']
            or decoded['created_at'] + decoded['expiry'] < terms['expires_at'] + 60
            or terms['expires_at'] <= int(time.time())):
        raise ValueError('routed quote invoice or expiry differs')
    routed.preflight(copy.deepcopy(state), decoded, MIN_CLTV, rpc)


def create(config, invoice, btc_sats, directory):
    if btc_sats is not None:
        raise ValueError('routed quote price comes from the oracle')
    ids = identities(config)  # Reject live networks before any mutation.
    decoded = RPC.call(config['xbt_cli'], 'decode', invoice)
    routed.invoice(decoded)
    now = int(time.time())
    expires = min(now + 120, decoded['created_at'] + decoded['expiry'] - 60)
    if expires < now + 30:
        raise ValueError('XBT invoice needs at least 90 seconds remaining')
    if any(p['payment_hash'] == decoded['payment_hash']
           for p in RPC.call(config['xbt_cli'], 'listsendpays')['payments']):
        raise ValueError('XBT invoice already has an outgoing attempt')
    route, policy = routed.plan(config['xbt_cli'], decoded, ids[1], RPC.call,
                                max_fee_msat=config['max_xbt_routing_fee_msat'])
    ticker, book = oracle.fetch('ticker'), oracle.fetch('orderbook')
    audit = pricing(config, ticker, book, int(time.time() * 1000))
    terms = dict(payment_hash=decoded['payment_hash'], payment_secret=secrets.token_hex(32),
                 btc_amount_msat=audit['btc_sats'] * 1000, xbt_amount_msat=routed.AMOUNT,
                 xbt_invoice=invoice, expires_at=expires, min_cltv_delta=MIN_CLTV,
                 max_cltv_delta=2000, btc_channel_policy=incoming_btc.POLICY)
    state = dict(profile='regtest', phase='prepared', quote_gate=True,
                 btc_cli=config['btc_cli'], xbt_cli=config['xbt_cli'], node_ids=ids,
                 btc_node_id=ids[0], xbt_routing=routed.MODE,
                 btc_channel_policy=incoming_btc.POLICY,
                 payment_hash=decoded['payment_hash'], payment_secret=decoded['payment_secret'],
                 xbt_invoice=invoice, xbt_amount_msat=routed.AMOUNT,
                 btc_amount_msat=terms['btc_amount_msat'], route=route, xbt_route_policy=policy)
    routed.preflight(state, decoded, MIN_CLTV, RPC.call)
    incoming_btc.preflight(config, terms['btc_amount_msat'], RPC.call)
    data = dict(config=copy.deepcopy(config), node_ids=ids, terms=terms, controller=state, oracle=audit)
    validate_quote(data)
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    save(directory/'quote.json', data)  # Before registration or signing.


def publish(directory):
    path = directory/'quote.json'
    data = private_load(path)
    if identities(data['config']) != data['node_ids']:
        raise ValueError('routed operator identity changed')
    preflight(data)
    if 'btc_invoice' in data:
        return
    terms, cli = data['terms'], data['config']['btc_cli']
    incoming_btc.preflight(data['config'], terms['btc_amount_msat'], RPC.call)
    if RPC.call(cli, 'xbt-register', json.dumps(terms)) != {'registered': True}:
        raise RuntimeError('quote registration failed')
    unsigned = unsigned_invoice(terms['payment_hash'], terms['payment_secret'], terms['btc_amount_msat'],
                                terms['expires_at'] - int(time.time()), currency='bcrt', final_cltv=INVOICE_CLTV)
    signed = RPC.call(cli, 'signinvoice', unsigned)['bolt11']
    decoded = RPC.call(cli, 'decode', signed)
    expected = dict(valid=True, currency='bcrt', payee=data['node_ids'][0],
                    payment_hash=terms['payment_hash'], payment_secret=terms['payment_secret'],
                    amount_msat=terms['btc_amount_msat'], min_final_cltv_expiry=INVOICE_CLTV)
    if any(decoded.get(k) != v for k, v in expected.items()):
        raise RuntimeError('signed BTC invoice differs from routed quote')
    data['btc_invoice'] = signed
    save(path, data)
