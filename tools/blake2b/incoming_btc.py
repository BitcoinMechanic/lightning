"""Pin the actual incoming BTC funding output before an unbound quote spends.

Publication only checks that an eligible channel exists. It does not reserve
liquidity or promise that a future payment will arrive on that channel.
"""
import copy
import re

import live_pilot as pilot
from quote_refusal import QuoteRefused

POLICY = 'any-normal-v1'
PIN_FIELDS = ('channel_id', 'funding_txid', 'funding_outnum')


def enabled(state):
    required = state.get('profile') in (pilot.PROFILE_MARKET_ANY, pilot.PROFILE_ROUTED)
    if required or 'btc_channel_policy' in state:
        if state.get('btc_channel_policy') != POLICY or 'btc_channel' in state:
            raise RuntimeError('incoming channel policy mismatch')
        return True
    return False


def preflight(config, amount, rpc):
    for c in rpc(config['btc_cli'], 'listpeerchannels')['channels']:
        if (c.get('state') != 'CHANNELD_NORMAL' or not c.get('peer_connected')
                or c.get('htlcs') or not c.get('short_channel_id')
                or c.get('receivable_msat', 0) < amount):
            continue
        try:
            pilot.require_untrimmed(c, amount)
        except (KeyError, TypeError, RuntimeError):
            continue
        return
    raise QuoteRefused('btc_channel_unavailable')


def validate_state(state):
    if not enabled(state):
        raise RuntimeError('incoming pin requires explicit policy')
    binding = state['btc_binding']
    pin = state['btc_incoming_pin']
    if (not isinstance(binding, list) or len(binding) != 2
            or not isinstance(binding[0], str) or not binding[0]
            or type(binding[1]) is not int or binding[1] < 0
            or set(pin) != set(PIN_FIELDS) | {'binding', 'payment_hash', 'amount_msat', 'expiry'}
            or pin['binding'] != binding or pin['payment_hash'] != state['payment_hash']
            or not isinstance(pin['channel_id'], str) or not re.fullmatch('[0-9a-f]{64}', pin['channel_id'])
            or not isinstance(pin['funding_txid'], str) or not re.fullmatch('[0-9a-f]{64}', pin['funding_txid'])
            or type(pin['funding_outnum']) is not int or not 0 <= pin['funding_outnum'] <= 65535
            or type(pin['amount_msat']) is not int or pin['amount_msat'] <= 0
            or pin['amount_msat'] != state['btc_amount_msat']
            or type(pin['expiry']) is not int or pin['expiry'] <= 0):
        raise RuntimeError('incoming funding pin differs from controller')


def pinned_channel(state, channels):
    validate_state(state)
    pin = state['btc_incoming_pin']
    matches = [c for c in channels if c.get('channel_id') == pin['channel_id']]
    if len(matches) != 1 or any(matches[0].get(k) != pin[k] for k in PIN_FIELDS):
        raise RuntimeError('incoming funding output changed or missing')
    return matches[0]


def check_spend(state, info, rpc):
    c = pinned_channel(state, rpc(state['btc_cli'], 'listpeerchannels')['channels'])
    pin = state['btc_incoming_pin']
    if (info.get('btc_channel_policy') != POLICY or info.get('btc_channel') is not None
            or info.get('payment_hash') != state['payment_hash']
            or info.get('binding') != state['btc_binding']
            or info.get('btc_amount_msat') != pin['amount_msat']
            or info.get('cltv_expiry') != pin['expiry']):
        raise RuntimeError('held quote differs from incoming funding pin')
    matches = [h for h in c.get('htlcs', []) if h.get('id') == state['btc_binding'][1]
               and h.get('direction') == 'in' and h.get('payment_hash') == state['payment_hash']]
    if (c.get('short_channel_id') != state['btc_binding'][0]
            or c.get('state') != 'CHANNELD_NORMAL' or not c.get('peer_connected')
            or len(matches) != 1 or matches[0].get('local_trimmed', False)
            or matches[0].get('state') != 'RCVD_ADD_ACK_REVOCATION'
            or matches[0].get('amount_msat') != pin['amount_msat']
            or matches[0].get('expiry') != pin['expiry']):
        raise RuntimeError('incoming BTC HTLC not committed and enforceable')
    pilot.require_untrimmed(c, pin['amount_msat'])


def bind(state, rpc):
    """Return a new checkpoint; caller must durably save it before submission."""
    if not enabled(state) or 'btc_incoming_pin' in state:
        raise RuntimeError('incoming funding pin must be created exactly once')
    channels = rpc(state['btc_cli'], 'listpeerchannels')['channels']
    matches = [c for c in channels if c.get('short_channel_id') == state['btc_binding'][0]]
    if len(matches) != 1:
        raise RuntimeError('incoming channel missing or ambiguous')
    info = rpc(state['btc_cli'], 'xbt-spend-info', state['payment_hash'])
    prepared = copy.deepcopy(state)
    prepared['btc_incoming_pin'] = dict(
        binding=copy.deepcopy(state['btc_binding']), payment_hash=state['payment_hash'],
        amount_msat=state['btc_amount_msat'], expiry=info['cltv_expiry'],
        **{k: matches[0][k] for k in PIN_FIELDS})
    check_spend(prepared, info, rpc)
    return prepared
