"""Explicit public/hinted-route XBT delivery fixture. Regtest only; no live policy."""
from reverse_route import plan_with_hints, validate
from reverse_metadata import invoice_metadata
import receive_bounds as bounds

MODE = 'public-xbt-regtest-v1'
AMOUNT = 100000000


def enabled(state):
    if state.get('profile') == 'live-routed-receive-v1' or state.get('xbt_routing') == 'bounded-xbt-live-v1':
        from live_receive import validate_state
        validate_state(state)
        return True
    if 'xbt_routing' not in state:
        return False
    if (state.get('xbt_routing') not in (MODE, bounds.MODE) or state.get('profile', 'regtest') != 'regtest'
            or state.get('quote_gate') is not True):
        raise ValueError('unsupported outgoing XBT routing profile')
    return True


def invoice(decoded):
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != 'xbtrt' or decoded.get('amount_msat') != AMOUNT
            or type(decoded.get('min_final_cltv_expiry')) is not int
            or not 0 < decoded['min_final_cltv_expiry'] <= 40
            or not decoded.get('payment_secret') or invoice_metadata(decoded) is not None):
        raise ValueError('unsupported routed XBT fixture invoice')


def plan(cli, decoded, source, rpc, max_fee_msat=10000):
    invoice(decoded)
    policy = dict(source=source, destination=decoded['payee'], max_fee_msat=max_fee_msat,
                  max_delay=80, max_hops=4, final_cltv=40)
    # Share the currency-independent route conversion/limits, not the BTC
    # invoice validator. Signed XBT invoice fields are never rewritten.
    return plan_with_hints(cli, AMOUNT, policy, decoded.get('routes', []), rpc)


def verify(state, rpc):
    if not enabled(state):
        return
    if state['xbt_routing'] == 'bounded-xbt-live-v1':
        from live_receive import verify_state
        verify_state(state, rpc)
        return
    policy = state['xbt_route_policy']
    validate(state['route'], state['xbt_amount_msat'], policy)
    if state['xbt_amount_msat'] != AMOUNT:
        raise ValueError('routed fixture amount changed')
    if state['xbt_routing'] == bounds.MODE:
        bounds.validate(state)
    for role, network, expected in (('btc_cli', 'regtest', state['btc_node_id']),
                                     ('xbt_cli', 'xbt-regtest', policy['source'])):
        info = rpc(state[role], 'getinfo')
        if info['network'] != network or info['id'] != expected:
            raise ValueError('routed fixture node identity or network changed')
        if any(k.startswith('warning_') for k in info):
            raise ValueError('routed fixture node reports a readiness warning')
    if state['phase'] != 'prepared':
        pin = state['xbt_first_hop']
        if pin['short_channel_id'] != state['route'][0]['channel'] or pin['peer_id'] != state['route'][0]['id']:
            raise ValueError('saved outgoing first hop differs')


def preflight(state, decoded, remaining, rpc):
    if state.get('xbt_routing') == 'bounded-xbt-live-v1':
        from live_receive import pre_spend
        return pre_spend(state, decoded, remaining, rpc)
    invoice(decoded)
    policy = state['xbt_route_policy']
    validate(state['route'], AMOUNT, policy)
    if (policy['destination'] != decoded['payee']
            or policy['final_cltv'] < decoded['min_final_cltv_expiry']
            or remaining < state['route'][0]['delay'] + 60):
        raise ValueError('routed destination or fresh incoming margin differs')
    first = state['route'][0]
    channels = [c for c in rpc(state['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == first['channel'] and c.get('peer_id') == first['id']]
    if (len(channels) != 1 or channels[0].get('state') != 'CHANNELD_NORMAL'
            or channels[0].get('peer_connected') is not True or channels[0].get('htlcs')
            or channels[0].get('spendable_msat', 0) < first['amount_msat']):
        raise ValueError('routed first hop lacks ready liquidity including fees')
    pin = {k: channels[0][k] for k in ('short_channel_id', 'peer_id', 'channel_id', 'funding_txid', 'funding_outnum')}
    if 'xbt_first_hop' in state and state['xbt_first_hop'] != pin:
        raise ValueError('outgoing funding pin changed')
    if state['xbt_routing'] == bounds.MODE:
        bounds.preflight(state, remaining, channels[0], rpc)
    state['xbt_first_hop'] = pin  # Persisted with outgoing_started before sendpay.


def check_payment(state, payment):
    amount = state['xbt_amount_msat'] if state.get('xbt_routing') == 'bounded-xbt-live-v1' else AMOUNT
    for key, value in dict(payment_hash=state['payment_hash'], amount_msat=amount,
                           amount_sent_msat=state['route'][0]['amount_msat'],
                           destination=state['xbt_route_policy']['destination'],
                           bolt11=state['xbt_invoice']).items():
        if payment.get(key) != value:
            raise RuntimeError('routed outgoing payment differs from original intent')
