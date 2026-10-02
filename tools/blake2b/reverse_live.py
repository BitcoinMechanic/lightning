"""Bounded reverse pilot integration. Live activation requires private opt-in.

The live profile is disabled by default; reverse_activation scopes explicit
service opt-in. Test profiles retain real regtest identities. Never retry
ambiguous spending automatically.
"""
import re
import time

from reverse_metadata import invoice_metadata
from reverse_route import validate, hinted_tail
from reverse_timing import proposal, pre_spend_report
from reverse_policy import inspect_remote_policies
from live_pilot import require_reserves, require_untrimmed

PROFILE = 'reverse-live-v1'
SERVICE_REGTEST = 'reverse-service-regtest-v1'
LIVE_EXECUTION_ENABLED = False


def is_service(state):
    return state.get('profile') in (PROFILE, SERVICE_REGTEST)


def networks(profile):
    if profile == PROFILE:
        return dict(btc='bitcoin', xbt='xbt', btc_currency='bc', xbt_currency='xbt')
    if profile == SERVICE_REGTEST:
        return dict(btc='regtest', xbt='xbt-regtest', btc_currency='bcrt', xbt_currency='xbtrt')
    raise ValueError('unknown reverse service profile')


def enabled(profile=PROFILE):
    networks(profile)  # Unknown profiles never bypass activation.
    from reverse_activation import ACTIVE
    if profile == PROFILE and not (LIVE_EXECUTION_ENABLED or ACTIVE.get()):
        raise RuntimeError('live reverse activation is disabled')


def private_final_allowed(route, decoded, policy, audit, lookup):
    """Only one unavailable policy, the final hop in an exact signed tail."""
    if audit['remote_btc_policy_violations'] or audit['remote_btc_policy_hops_unknown'] != 1:
        return False
    if len(route) < 2:
        return False
    final = route[-1]
    rows = lookup(final['channel'])['channels']
    if any(c.get('short_channel_id') == final['channel']
           and c.get('source') == route[-2]['id']
           and c.get('destination') == final['id'] for c in rows):
        return False
    for hint in decoded.get('routes', []):
        try:
            tail, entry, _, _ = hinted_tail(hint, decoded['amount_msat'], policy)
        except ValueError:
            continue
        start = len(route) - len(tail)
        source = policy['source'] if start == 0 else route[start-1]['id'] if start > 0 else None
        if source == entry and start >= 0 and route[start:] == tail:
            return True
    return False


def validate_terms(terms):
    required = {'profile', 'payment_hash', 'payment_secret', 'xbt_amount_msat',
                'btc_amount_msat', 'btc_invoice', 'xbt_channel', 'expires_at',
                'min_cltv_delta', 'max_cltv_delta', 'node_ids', 'payer_id',
                'route', 'routing', 'timing', 'allow_signed_private_final'}
    from incoming_xbt import unbound
    dynamic = unbound(terms)
    if dynamic:
        required -= {'xbt_channel', 'payer_id'}
        required.add('incoming_policy')
    if not isinstance(terms, dict) or set(terms) != required or terms['profile'] not in (PROFILE, SERVICE_REGTEST):
        raise ValueError('unsupported live reverse quote')
    for k in ('payment_hash', 'payment_secret'):
        if not isinstance(terms[k], str) or not re.fullmatch('[0-9a-f]{64}', terms[k]):
            raise ValueError('invalid live quote hash or secret')
    if (type(terms['btc_amount_msat']) is not int or terms['btc_amount_msat'] != 1500000
            or type(terms['xbt_amount_msat']) is not int
            or not 1000 <= terms['xbt_amount_msat'] <= 500000000
            or terms['xbt_amount_msat'] % 1000
            or terms['allow_signed_private_final'] is not True
            or type(terms['expires_at']) is not int
            or not isinstance(terms['btc_invoice'], str)
            or not terms['btc_invoice'].startswith('ln'+networks(terms['profile'])['btc_currency'])
            or (terms['profile'] == PROFILE and terms['btc_invoice'].startswith('lnbcrt'))
            or (not dynamic and not re.fullmatch(r'[0-9]+x[0-9]+x[0-9]+', terms['xbt_channel']))):
        raise ValueError('live reverse quote outside pilot bounds')
    ids = terms['node_ids']
    all_ids = ids + ([] if dynamic else [terms['payer_id']]) if isinstance(ids, list) else []
    if (not isinstance(ids, list) or len(ids) != 2
            or len(set(all_ids)) != len(all_ids)
            or any(not isinstance(i, str) or not re.fullmatch('0[23][0-9a-f]{64}', i) for i in all_ids)):
        raise ValueError('invalid live quote identities')
    route, policy = terms['route'], terms['routing']
    validate(route, terms['btc_amount_msat'], policy, _inspection=True)
    if (policy['source'] != ids[1] or policy['destination'] in ids
            or policy['max_fee_msat'] > 30000 or policy['max_delay'] > 576):
        raise ValueError('live reverse route outside pilot bounds')
    timing = proposal(route[0]['delay'])
    if (terms['timing'] != timing or not timing['fits_default_cltv_budget']
            or type(terms['min_cltv_delta']) is not int
            or terms['min_cltv_delta'] != timing['minimum_xbt_remaining_blocks']
            or type(terms['max_cltv_delta']) is not int or terms['max_cltv_delta'] != 2016):
        raise ValueError('live reverse timing binding changed')


def verify_state(state, rpc):
    enabled(state['profile'])
    net = networks(state['profile'])
    terms = state['reverse_quote']
    if terms.get('profile') != state['profile']:
        raise RuntimeError('reverse service profile binding changed')
    validate_terms(terms)
    pairs = {'payment_hash': 'payment_hash', 'btc_invoice': 'btc_invoice',
             'btc_amount_msat': 'btc_amount_msat', 'xbt_amount_msat': 'xbt_amount_msat',
             'node_ids': 'node_ids', 'route': 'route', 'routing': 'routing'}
    if (any(state.get(k) != terms[v] for k, v in pairs.items())
            or ('xbt_channel' in terms and state['xbt_binding'][0] != terms['xbt_channel'])
            or any(state.get(k) is not True for k in ('durable_gate', 'xbt_onchain_claim', 'xbt_deadline_guard'))):
        raise RuntimeError('live reverse controller binding changed')
    from incoming_xbt import unbound, validate_pin
    if unbound(terms):
        validate_pin(state)
    for cli, network, node in ((state['xbt_cli'], net['xbt'], terms['node_ids'][0]),
                               (state['btc_cli'], net['btc'], terms['node_ids'][1])):
        info = rpc(cli, 'getinfo')
        if info['network'] != network or info['id'] != node:
            raise RuntimeError('live reverse node identity mismatch')


def preflight(state, decoded, incoming_channel, outgoing_channel, rpc):
    terms = state['reverse_quote']
    if (('payer_id' in terms and incoming_channel['peer_id'] != terms['payer_id'])
            or decoded.get('currency') != networks(state['profile'])['btc_currency']
            or invoice_metadata(decoded) != state['btc_payment_metadata']):
        raise RuntimeError('live reverse invoice or incoming peer mismatch')
    infos = [rpc(state[k], 'getinfo') for k in ('btc_cli', 'xbt_cli')]
    if any(any(k.startswith('warning_') for k in info) for info in infos):
        raise RuntimeError('live reverse node reports a warning')
    report = pre_spend_report(terms['timing'], btc_height=infos[0]['blockheight'],
                             xbt_height=infos[1]['blockheight'], xbt_expiry=state['xbt_expiry'])
    if not report['model_margin_met']:
        raise RuntimeError('live reverse incoming timing margin insufficient')
    if terms['expires_at'] <= int(time.time()):
        raise RuntimeError('live reverse quote expired before spending')
    require_reserves(state, rpc)
    require_untrimmed(incoming_channel, state['xbt_amount_msat'])
    require_untrimmed(outgoing_channel, state['route'][0]['amount_msat'])
    if any(h.get('local_trimmed') is True for h in incoming_channel.get('htlcs', [])
           if h.get('id') == state['xbt_binding'][1] and h.get('direction') == 'in'):
        raise RuntimeError('incoming XBT HTLC is trimmed')
    lookup = lambda scid: rpc(state['btc_cli'], 'listchannels', scid)
    audit = inspect_remote_policies(state['route'], lookup)
    if audit['remote_btc_policy_violations']:
        raise RuntimeError('live reverse remote policy changed')
    if audit['remote_btc_policy_hops_unknown'] and not private_final_allowed(
            state['route'], decoded, state['routing'], audit, lookup):
        raise RuntimeError('unknown remote policy is not signed private final hop')
    state['outgoing_channel'] = {k: outgoing_channel[k] for k in
        ('channel_id', 'funding_txid', 'funding_outnum', 'peer_id')}
    state['timing_pre_spend'] = report
    state['btc_submission_height'] = infos[0]['blockheight']


def monitor_pending(path, state, rpc, save):
    """Observe original BTC expiry; never infer failure from height or elapsed time."""
    from reverse_timing import pending_report
    btc = rpc(state['btc_cli'], 'getinfo')
    xbt = rpc(state['xbt_cli'], 'getinfo')
    net = networks(state['profile'])
    if (btc['network'] != net['btc'] or btc['id'] != state['node_ids'][1]
            or xbt['network'] != net['xbt'] or xbt['id'] != state['node_ids'][0]):
        raise RuntimeError('pending reverse timing identity mismatch')
    pin = state['outgoing_channel']
    channels = [c for c in rpc(state['btc_cli'], 'listpeerchannels')['channels']
                if c.get('channel_id') == pin['channel_id']]
    if len(channels) != 1 or any(channels[0].get(k) != v for k, v in pin.items()):
        raise RuntimeError('pending BTC channel pin unavailable')
    htlcs = [h for h in channels[0].get('htlcs', [])
             if h.get('direction') == 'out' and h.get('payment_hash') == state['payment_hash']]
    if len(htlcs) > 1:
        raise RuntimeError('multiple BTC HTLCs for reverse attempt')
    if htlcs:
        h = htlcs[0]
        if (h.get('amount_msat') != state['route'][0]['amount_msat']
                or type(h.get('expiry')) is not int or h['expiry'] <= 0):
            raise RuntimeError('BTC HTLC timing or amount mismatch')
        observation = dict(id=h['id'], expiry=h['expiry'])
        if state.get('btc_htlc') is not None and state['btc_htlc'] != observation:
            raise RuntimeError('original BTC HTLC changed')
        state['btc_htlc'] = observation
    expiry = (state['btc_htlc']['expiry'] if 'btc_htlc' in state
              else state['timing_pre_spend']['btc_planning_expiry_upper'])
    report = pending_report(btc_height=btc['blockheight'], btc_htlc_expiry=expiry,
                            xbt_height=xbt['blockheight'], xbt_htlc_expiry=state['xbt_expiry'])
    report['btc_expiry_observed'] = 'btc_htlc' in state
    state['pending_timing'] = report
    save(path, state)
