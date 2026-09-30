"""Bounded variable-price live quotes; oracle consulted only during preparation."""
import hashlib
import json
import time

import live_pilot as pilot
from neoxa_oracle import estimate, fetch

MAX_BTC_SATS = 10000
MAX_XBT_SATS = 500000


def policy(config):
    p = config['market']
    if set(p) != {'btc_channel', 'xbt_channel', 'xbt_peer', 'max_btc_sats',
                  'max_xbt_sats', 'margin_bps'}:
        raise ValueError('unexpected market policy fields')
    for key, limit in (('max_btc_sats', MAX_BTC_SATS), ('max_xbt_sats', MAX_XBT_SATS)):
        if type(p[key]) is not int or not 0 < p[key] <= limit:
            raise ValueError('market amount cap outside experimental bounds')
    if type(p['margin_bps']) is not int or not 0 <= p['margin_bps'] <= 500:
        raise ValueError('market margin must be 0..500 basis points')
    for key in ('btc_channel', 'xbt_channel', 'xbt_peer'):
        if not isinstance(p[key], str) or not p[key]:
            raise ValueError('market policy needs explicit channel and receiver bindings')
    return p


def digest(audit):
    return hashlib.sha256(json.dumps(audit, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def state_amounts(state):
    p = policy(state)
    btc, xbt = state['btc_amount_msat'], state['xbt_amount_msat']
    for value, maximum in ((btc, p['max_btc_sats']), (xbt, p['max_xbt_sats'])):
        if type(value) is not int or value % 1000 or not 0 < value <= maximum * 1000:
            raise RuntimeError('market amount outside configured limits')
    audit = state['oracle']
    if (audit['btc_sats'] * 1000 != btc or audit['xbt_sats'] * 1000 != xbt
            or audit['margin_bps'] != p['margin_bps']
            or digest(audit) != state['oracle_digest']):
        raise RuntimeError('market price record mismatch')
    route = state['route']
    if (len(route) != 1 or route[0]['channel'] != p['xbt_channel']
            or route[0]['id'] != p['xbt_peer'] or route[0]['amount_msat'] != xbt
            or route[0]['delay'] != 40 or state['btc_channel'] != p['btc_channel']):
        raise RuntimeError('market route binding mismatch')
    return btc, xbt


def channels(config, amount, btc_amount, rpc):
    p = policy(config)
    pilot.incoming_preflight(config, p['btc_channel'], rpc, amount_msat=btc_amount)
    matches = [c for c in rpc(config['xbt_cli'], 'listpeerchannels')['channels']
               if c.get('short_channel_id') == p['xbt_channel'] and c['peer_id'] == p['xbt_peer']]
    if (len(matches) != 1 or matches[0]['state'] != 'CHANNELD_NORMAL'
            or not matches[0]['peer_connected'] or matches[0].get('htlcs')
            or matches[0]['spendable_msat'] < amount):
        raise RuntimeError('bound XBT channel not ready or lacks balance')
    pilot.require_untrimmed(matches[0], amount)
    pilot.require_reserves(config, rpc)


def prepare(config, decoded, channel, rpc):
    p = policy(config)
    amount = decoded['amount_msat']
    if (amount % 1000 or not 0 < amount <= p['max_xbt_sats'] * 1000
            or decoded['payee'] != p['xbt_peer']
            or channel['short_channel_id'] != p['xbt_channel']):
        raise ValueError('receiver, channel or amount outside market policy')
    start = time.monotonic()
    ticker, book = fetch('ticker'), fetch('orderbook')
    if time.monotonic()-start > 15:
        raise ValueError('oracle snapshot acquisition too slow')
    audit = estimate(ticker, book, amount//1000, now_ms=time.time_ns()//1000000,
                     margin_bps=p['margin_bps'])
    if not 0 < audit['btc_sats'] <= p['max_btc_sats']:
        raise ValueError('oracle BTC amount exceeds operator cap')
    channels(config, amount, audit['btc_sats'] * 1000, rpc)
    return audit


def publication(data, rpc):
    state, terms = data['controller'], data['terms']
    btc, xbt = state_amounts(state)
    if (terms['btc_amount_msat'] != btc or terms['xbt_amount_msat'] != xbt
            or terms['oracle_digest'] != state['oracle_digest']
            or terms['controller_id'] != state['controller_id']
            or terms['btc_channel'] != state['btc_channel']
            or terms['payment_hash'] != state['payment_hash']
            or terms['xbt_invoice'] != state['xbt_invoice']
            or terms['pilot'] != pilot.PROFILE_MARKET):
        raise RuntimeError('market terms differ from controller checkpoint')
    now = time.time_ns() // 1000000
    if not 0 <= now-state['oracle']['ticker_computed_at_ms'] <= 30000:
        raise RuntimeError('price too old to publish; preserve draft and let it expire')
    channels(data['config'], xbt, btc, rpc)
