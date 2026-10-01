"""Pure candidate timing model for a bounded live reverse pilot.

No RPCs, quotes, payments or channel closes. Independent chains have no bounded
relative progress: this is an explicit risk budget, not an atomicity guarantee.
Neither this report nor meeting its margins enables live execution.
"""

MODEL = 'reverse-timing-candidate-v2'
# Both chains target ten-minute blocks. This is an expected pace, not a
# maximum rate or a guarantee; recovery headroom is accounted for separately.
EXPECTED_XBT_PER_BTC = 1
BTC_SUBMISSION_SLACK = 6
XBT_RECOVERY_RESERVE = 144
XBT_QUOTE_DRIFT = 24
XBT_MAX_DELTA = 2016
MAX_ROUTE_DELAY = (XBT_MAX_DELTA - XBT_RECOVERY_RESERVE - XBT_QUOTE_DRIFT) // EXPECTED_XBT_PER_BTC - BTC_SUBMISSION_SLACK


def integer(value, name, low=0, high=499999999):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid timing '+name)
    return value


def proposal(btc_route_delay):
    delay = integer(btc_route_delay, 'route delay', 1, 2016)
    minimum = EXPECTED_XBT_PER_BTC * (delay + BTC_SUBMISSION_SLACK) + XBT_RECOVERY_RESERVE
    invoice_delta = minimum + XBT_QUOTE_DRIFT
    return dict(model=MODEL, status='proposal_only', live_payment_enabled=False,
                relative_chain_progress_guaranteed=False,
                expected_xbt_blocks_per_btc_block=EXPECTED_XBT_PER_BTC,
                btc_route_delay=delay, btc_submission_slack_blocks=BTC_SUBMISSION_SLACK,
                xbt_recovery_reserve_blocks=XBT_RECOVERY_RESERVE,
                xbt_quote_drift_blocks=XBT_QUOTE_DRIFT,
                minimum_xbt_remaining_blocks=minimum,
                proposed_xbt_invoice_cltv=invoice_delta,
                maximum_xbt_remaining_blocks=XBT_MAX_DELTA,
                maximum_supported_btc_route_delay=MAX_ROUTE_DELAY,
                fits_default_cltv_budget=invoice_delta <= XBT_MAX_DELTA)


def pre_spend_report(candidate, *, btc_height, xbt_height, xbt_expiry):
    """Recheck a proposal against fresh heights and the actual held expiry.

Future integration must authenticate network/node/HTLC identities and obtain
fresh heights. This pure helper cannot verify either of those prerequisites.
It never compares absolute heights between the chains.
"""
    if not isinstance(candidate, dict) or candidate != proposal(candidate.get('btc_route_delay')):
        raise ValueError('timing proposal changed')
    btc_height = integer(btc_height, 'BTC height')
    xbt_height = integer(xbt_height, 'XBT height')
    xbt_expiry = integer(xbt_expiry, 'XBT expiry')
    remaining = xbt_expiry - xbt_height
    reasons = []
    if not candidate['fits_default_cltv_budget']:
        reasons.append('route exceeds proposed timing budget')
    if remaining < candidate['minimum_xbt_remaining_blocks']:
        reasons.append('incoming XBT margin below proposed minimum')
    if remaining > XBT_MAX_DELTA:
        reasons.append('incoming XBT expiry exceeds proposed maximum')
    return dict(model=MODEL, model_margin_met=not reasons, reasons=reasons,
                xbt_remaining_blocks=remaining,
                # An upper planning reference, NOT an observed outgoing HTLC.
                btc_planning_expiry_upper=btc_height + candidate['btc_route_delay'] + BTC_SUBMISSION_SLACK,
                live_payment_enabled=False)


def pending_report(*, btc_height, btc_htlc_expiry, xbt_height, xbt_htlc_expiry):
    """Describe an unresolved original attempt using its actual expiries.

An elapsed BTC expiry is NOT definitive payment failure. Breach flags never
permit failing XBT, resending BTC, or forgetting the original attempt.
"""
    for name, value in (('BTC height', btc_height), ('BTC expiry', btc_htlc_expiry),
                        ('XBT height', xbt_height), ('XBT expiry', xbt_htlc_expiry)):
        integer(value, name)
    btc_remaining = max(0, btc_htlc_expiry - btc_height)
    xbt_remaining = xbt_htlc_expiry - xbt_height
    required = EXPECTED_XBT_PER_BTC * btc_remaining + XBT_RECOVERY_RESERVE
    return dict(model=MODEL, btc_remaining_blocks=btc_remaining,
                xbt_remaining_blocks=xbt_remaining,
                model_required_xbt_remaining_blocks=required,
                model_margin_breached=xbt_remaining < required,
                recovery_reserve_reached=xbt_remaining <= XBT_RECOVERY_RESERVE,
                btc_expiry_reached=btc_htlc_expiry <= btc_height,
                outcome_still_requires_reconciliation=True,
                permits_xbt_failure=False, permits_btc_resend=False,
                live_payment_enabled=False)
