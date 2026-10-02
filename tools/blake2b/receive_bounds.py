"""Candidate forward spending policy. Execution remains regtest-only.

Independent chain progress is never guaranteed by these block margins.
"""
from live_pilot import require_reserves, require_untrimmed
from reverse_policy import inspect_remote_policies

MODE = 'bounded-xbt-regtest-v1'
PROFILE = 'bounded-receive-regtest-v1'


def timing(delay):
    if type(delay) is not int or not 1 <= delay <= 2016:
        raise ValueError('invalid outgoing XBT delay')
    minimum = delay + 6 + 144
    return dict(model='forward-timing-candidate-v1', status='proposal_only',
                live_payment_enabled=False, relative_chain_progress_guaranteed=False,
                expected_btc_blocks_per_xbt_block=1, xbt_route_delay=delay,
                xbt_submission_slack_blocks=6, btc_recovery_reserve_blocks=144,
                btc_quote_drift_blocks=24, minimum_btc_remaining_blocks=minimum,
                proposed_btc_invoice_cltv=minimum+24, maximum_btc_remaining_blocks=2016,
                fits_default_cltv_budget=minimum+24 <= 2016)


def validate(state):
    expected = timing(state['route'][0]['delay'])
    if (state.get('profile') != 'regtest' or state.get('xbt_routing') != MODE
            or state.get('quote_gate') is not True or state.get('xbt_timing') != expected
            or not expected['fits_default_cltv_budget']):
        raise ValueError('bounded receiving policy or timing changed')
    return expected


def gate(state, info):
    policy = validate(state)
    if (info.get('min_cltv_delta') != policy['minimum_btc_remaining_blocks']
            or info.get('max_cltv_delta') != policy['maximum_btc_remaining_blocks']):
        raise ValueError('held quote timing differs from pinned routed policy')


def preflight(state, remaining, channel, rpc):
    policy = validate(state)
    if (type(remaining) is not int or not policy['minimum_btc_remaining_blocks']
            <= remaining <= policy['maximum_btc_remaining_blocks']):
        raise ValueError('fresh BTC margin outside bounded routed policy')
    require_reserves(state, rpc)
    require_untrimmed(channel, state['route'][0]['amount_msat'])
    audit = inspect_remote_policies(state['route'],
                                   lambda scid: rpc(state['xbt_cli'], 'listchannels', scid))
    if not audit['remote_btc_htlc_limits_passed']:
        raise ValueError('remote XBT limits unavailable or route policy changed')
