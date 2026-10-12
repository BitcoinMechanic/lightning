"""Immutable cross-chain terms and admission checks; disposable regtest only.

This is policy/data, not an executor or proof of human approval. The future
worker must durably record the exact confirmed digest before remote enrollment,
and must preserve evidence before its sole outgoing send. No legacy conversion.
"""
import copy
import secrets

import bounded_authority as ba
import customer_invoice as invoice
import customer_remote as remote
import held_gate as hg
import market_quote as market
import route_plan as rp

FORMAT = 'swap-cross-contract-v1'
RESERVE_MSAT = 50000000
RECOVERY_BLOCKS = 144
PROPAGATION_BLOCKS = 6
OBSERVATION_SECONDS = 30


def grant_pair(grants, direction):
    hg.need(direction in ('forward', 'reverse') and type(grants) is dict
            and set(grants) == {'incoming', 'outgoing'}, 'contract_grants_invalid')
    profiles = (ba.ci.BTC, ba.ci.PRIVKEYIO_XBT) if direction == 'forward' else (ba.ci.PRIVKEYIO_XBT, ba.ci.BTC)
    for kind, profile in zip(('incoming', 'outgoing'), profiles):
        g = grants[kind]; ba.grant(g, hg.binding(g['chain']))
        hg.need(g['kind'] == kind and g['chain']['profile'] == profile, 'contract_chain_or_kind_changed')
    hg.need(grants['incoming']['chain']['node_id'] != grants['outgoing']['chain']['node_id'], 'contract_nodes_not_distinct')


def validate(c):
    hg.need(type(c) is dict and set(c) == {'schema', 'format', 'direction', 'nonce', 'created_at',
        'pay_until', 'grants', 'incoming', 'outgoing', 'pricing'}, 'contract_fields_invalid')
    hg.need(type(c['schema']) is int and c['schema'] == 1 and c['format'] == FORMAT
            and ba.ci.hex_field(c['nonce'], 64), 'contract_format_refused')
    grant_pair(c['grants'], c['direction'])
    plan, quote = c['outgoing'], c['incoming']
    for kind, data in (('incoming', quote), ('outgoing', plan)):
        ba.payload(data, c['grants'][kind])
    pricing = market.quote(c['pricing'])
    hg.need(pricing['direction'] == c['direction'] and pricing['recipient_msat'] == plan['amount_msat']
            and pricing['routing_fee_msat'] == plan['route'][0]['amount_msat']-plan['amount_msat'], 'contract_price_changed')
    incoming_asset, outgoing_asset = ('btc', 'xbt') if c['direction'] == 'forward' else ('xbt', 'btc')
    hg.need(quote['amount_msat'] == pricing[incoming_asset+'_msat']
            and plan['amount_msat'] == pricing[outgoing_asset+'_msat'], 'contract_amount_changed')
    hg.need(quote['payment_hash'] == plan['payment_hash'] and quote['payment_secret'] != plan['payment_secret'],
            'contract_hash_or_secret_changed')
    hg.need(hg.integer(c['created_at'], max(plan['created_at'], pricing['fetched_at_ms']//1000), pricing['expires_at']-1)
            and c['pay_until'] == pricing['expires_at'] == quote['expires_at']
            and plan['expires_at'] >= c['pay_until']+180, 'contract_expiry_invalid')
    minimum = max(plan['route'][0]['delay']+RECOVERY_BLOCKS+PROPAGATION_BLOCKS,
                  c['grants']['incoming']['limits']['min_cltv_delta'])
    hg.need(quote['min_cltv_delta'] == minimum and quote['max_cltv_delta'] ==
            c['grants']['incoming']['limits']['max_cltv_delta'] and minimum+24 <= quote['max_cltv_delta'],
            'contract_timing_changed')
    for g in c['grants'].values():
        hg.need(g['created_at'] <= c['created_at'] < c['pay_until'] <= g['expires_at'], 'contract_grant_expiry')
    return c


def create(grants, plan, pricing, *, now, payment_secret=None, nonce=None):
    pricing = copy.deepcopy(market.quote(pricing))
    grant_pair(grants, pricing['direction']); rp.validate_plan(plan)
    g = grants['incoming']
    quote = dict(chain=g['chain'], payment_hash=plan['payment_hash'],
        payment_secret=secrets.token_hex(32) if payment_secret is None else payment_secret, amount_msat=pricing[
            ('btc' if pricing['direction'] == 'forward' else 'xbt')+'_msat'],
        expires_at=pricing['expires_at'], approved_channels=g['channels'],
        min_cltv_delta=max(plan['route'][0]['delay']+RECOVERY_BLOCKS+PROPAGATION_BLOCKS, g['limits']['min_cltv_delta']),
        max_cltv_delta=g['limits']['max_cltv_delta'])
    return copy.deepcopy(validate(dict(schema=1, format=FORMAT, direction=pricing['direction'],
        nonce=secrets.token_hex(32) if nonce is None else nonce, created_at=now, pay_until=pricing['expires_at'],
        grants=grants, incoming=quote, outgoing=plan, pricing=pricing)))


def digest(c):
    return ba.digest(validate(c))


def inspection(c, kind, observed, now):
    g = c['grants'][kind]
    fields = {'format', 'checked_at', 'info', 'blockheight', 'reserve_msat'} | ({'hints'} if kind == 'incoming' else set())
    hg.need(type(observed) is dict and set(observed) == fields and observed['format'] == remote.FORMAT
            and hg.integer(observed['checked_at'], now-OBSERVATION_SECONDS, now)
            and hg.integer(observed['blockheight'], 0, 2**32-1)
            and hg.integer(observed['reserve_msat'], RESERVE_MSAT, 2**63-1), 'contract_inspection_unavailable')
    info = observed['info']
    hg.need(type(info) is dict and set(info) == {'format', 'grant', 'paused', 'current', 'remaining', 'reserved_msat', 'regtest_only'}
            and info['format'] == remote.FORMAT and ba.equal(info['grant'], g)
            and info['paused'] is False and info['current'] is True and info['regtest_only'] is True
            and hg.integer(info['remaining'], 0, g['limits']['max_swaps'])
            and hg.integer(info['reserved_msat'], 0, g['limits']['total_amount_msat']), 'contract_grant_changed')
    if kind == 'incoming': invoice.validate_hints(observed['hints'], c['incoming'])
    return info


def admission(c, observations, *, now, enrolled=False):
    validate(c)
    hg.need(type(enrolled) is bool and hg.integer(now, c['created_at'], c['pay_until']-1), 'contract_admission_expired')
    hg.need(type(observations) is dict and set(observations) == {'incoming', 'outgoing'}, 'contract_inspection_unavailable')
    for kind in ('incoming', 'outgoing'):
        info = inspection(c, kind, observations[kind], now); g = c['grants'][kind]
        hg.need(g['created_at'] <= now < g['expires_at'], 'contract_grant_expiry')
        if not enrolled:
            debit = ba.payload(c[kind], g)
            hg.need(info['remaining'] > 0 and info['reserved_msat']+debit <= g['limits']['total_amount_msat'],
                    'contract_budget_exhausted')
    return digest(c)


def confirm(c, confirmation, observations, *, now):
    hg.need(type(confirmation) is str and confirmation == digest(c), 'exact_confirmation_required')
    return admission(c, observations, now=now)


def held_margin(c, evidence, *, incoming_height):
    """Worker-side safety check. Does not send, release, or compare chain heights.

    The two chains have independent height counters. Incoming remaining blocks
    must cover the saved outgoing relative delay plus the fixed recovery margin.
    """
    validate(c); quote = c['incoming']; g = c['grants']['incoming']
    eid = ba.digest(dict(grant_id=ba.digest(g), payload=quote))
    hg.need(type(evidence) is dict and set(evidence) == {'entry_id', 'entry', 'status'}
            and evidence['entry_id'] == eid, 'contract_incoming_evidence_invalid')
    e = evidence['entry']; gid = ba.digest(g)
    ba.validate(dict(schema=1, format=ba.FORMAT, chain=g['chain'], current=dict(incoming=gid, outgoing=None),
        grants={gid:dict(terms=g, paused=False)}, entries={eid:e}), g['chain'])
    hg.need(ba.equal(e['payload'], quote) and e['grant_id'] == gid and 'incoming' in e
            and 'decision' not in e and 'terminal' not in e and evidence['status'] ==
                dict(kind='incoming', state='held', terminal=False, regtest_only=True, blocked=False),
            'contract_incoming_not_held')
    hg.need(hg.integer(incoming_height, 0, 2**32-1) and quote['min_cltv_delta'] <=
            e['incoming']['expiry']-incoming_height <= quote['max_cltv_delta'], 'contract_held_margin_insufficient')
    return copy.deepcopy(e['incoming'])


def review(c):
    validate(c)
    return dict(format=FORMAT, confirmation=digest(c), direction=c['direction'],
        btc_msat=c['pricing']['btc_msat'], xbt_msat=c['pricing']['xbt_msat'],
        routing_fee_msat=c['pricing']['routing_fee_msat'], markup_bps=c['pricing']['markup_bps'],
        expires_at=c['pay_until'], outgoing_hops=len(c['outgoing']['route']),
        outgoing_delay_blocks=c['outgoing']['route'][0]['delay'],
        incoming_channel='Detected when payment arrives', regtest_only=True)
