"""Read-only assertions for the routed incoming XBT regtest fixture."""
import hashlib

from reverse_route import query
from smoke_regtest import wait_until

RELAY_FEE_MSAT = 5000


def route_ready(result, source, relay, destination, amount, final_cltv):
    """Wait for the fixture's public route and its updated relay fee."""
    routes = result.get('routes', [])
    if len(routes) != 1:
        return False
    route = routes[0]
    path = route.get('path', [])
    if (route.get('amount_msat') != amount or route.get('final_cltv') != final_cltv
            or len(path) != 2):
        return False
    first, last = path
    return (
        first.get('node_id_in') == source
        and first.get('node_id_out') == relay
        and last.get('node_id_in') == relay
        and last.get('node_id_out') == destination
        and first.get('amount_in_msat') == amount + RELAY_FEE_MSAT
        and first.get('amount_out_msat') == amount + RELAY_FEE_MSAT
        and last.get('amount_in_msat') == amount + RELAY_FEE_MSAT
        and last.get('amount_out_msat') == amount
        and first.get('cltv_in') == first.get('cltv_out') == last.get('cltv_in')
        and type(last.get('cltv_in')) is int
        and final_cltv < last['cltv_in'] <= 2016
        and last.get('cltv_out') == final_cltv
    )


def wait_for_route(lab, payer, relay, incoming, terms):
    policy = dict(source=payer['id'], destination=incoming['id'], max_fee_msat=10000,
                  max_delay=2016, final_cltv=terms['timing']['proposed_xbt_invoice_cltv'])
    amount = terms['xbt_amount_msat']
    wait_until(lambda: route_ready(query(payer['cli'], amount, policy, lab.rpc),
                                   payer['id'], relay['id'], incoming['id'], amount,
                                   policy['final_cltv']), payer['proc'], timeout=90)


def receipt(rows, quote, operator_id, *, failed=False):
    """Check the original ordinary-pay result without issuing another payment."""
    if len(rows) != 1:
        raise AssertionError('expected exactly one payer result')
    row, terms = rows[0], quote['terms']
    if (row.get('bolt11') != quote['xbt_invoice']
            or row.get('destination') != operator_id
            or row.get('payment_hash') != terms['payment_hash']
            or row.get('status') != ('failed' if failed else 'complete')
            or ('amount_msat' in row and row['amount_msat'] != terms['xbt_amount_msat'])):
        raise AssertionError('payer result differs from fixture invoice')
    result = dict(outcome=row['status'], automatic_resubmission=False)
    if failed:
        if row.get('preimage') is not None:
            raise AssertionError('failed payment unexpectedly has a preimage')
        return result
    try:
        preimage = bytes.fromhex(row['preimage'])
    except (ValueError, TypeError, KeyError) as exc:
        raise AssertionError('missing or invalid payment proof') from exc
    if (len(preimage) != 32 or hashlib.sha256(preimage).hexdigest() != terms['payment_hash']
            or row.get('amount_sent_msat') != terms['xbt_amount_msat'] + RELAY_FEE_MSAT
            or row.get('amount_msat') != terms['xbt_amount_msat']
            or row.get('number_of_parts', 1) != 1):
        raise AssertionError('payment proof, part count or relay fee differs')
    return dict(result, xbt_sent_sats=row['amount_sent_msat']//1000,
                xbt_routing_fee_msat=RELAY_FEE_MSAT, matching_preimage_verified=True)
