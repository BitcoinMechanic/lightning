"""Bounded, single-part BTC route planning for the reverse regtest controller.

Uses the pinned CLN getroutes schema, not pay/xpay or an automatic retry loop.
Supports bounded BOLT11 route hints; no blinded paths or multipath payments.
"""
import json
import re
import subprocess


def limits(policy, *, _prefix=False):
    if set(policy) != {'source', 'destination', 'max_fee_msat', 'max_delay', 'max_hops', 'final_cltv'}:
        raise ValueError('unexpected reverse route policy')
    for key, low, high in (('max_fee_msat', 0, 10000), ('max_delay', 1, 80),
                            ('max_hops', 1, 4), ('final_cltv', 1, 80 if _prefix else 40)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ValueError('reverse route policy exceeds regtest limits')
    if (not all(isinstance(policy[k], str) and policy[k] for k in ('source', 'destination'))
            or policy['source'] == policy['destination'] or policy['final_cltv'] > policy['max_delay']):
        raise ValueError('invalid reverse route endpoints or delay bounds')


def validate(route, amount_msat, policy, *, _prefix=False):
    limits(policy, _prefix=_prefix)
    maximum = 100010000 if _prefix else 100000000
    if type(amount_msat) is not int or not 100000000 <= amount_msat <= maximum:
        raise ValueError('unsupported reverse routed fixture amount')
    if not isinstance(route, list) or not 1 <= len(route) <= policy['max_hops']:
        raise ValueError('reverse route hop count outside bounds')
    nodes, channels = {policy['source']}, set()
    previous_amount = previous_delay = None
    for hop in route:
        if set(hop) != {'id', 'channel', 'amount_msat', 'delay'}:
            raise ValueError('unexpected reverse route fields')
        if (not isinstance(hop['id'], str) or not hop['id'] or hop['id'] in nodes
                or not isinstance(hop['channel'], str)
                or not re.fullmatch(r'[0-9]+x[0-9]+x[0-9]+', hop['channel'])
                or hop['channel'] in channels):
            raise ValueError('invalid or looping reverse route')
        if (type(hop['amount_msat']) is not int or hop['amount_msat'] < amount_msat
                or type(hop['delay']) is not int or not policy['final_cltv'] <= hop['delay'] <= policy['max_delay']):
            raise ValueError('invalid reverse route amount or delay')
        if previous_amount is not None and (hop['amount_msat'] > previous_amount or hop['delay'] > previous_delay):
            raise ValueError('reverse route amounts or delays increase downstream')
        nodes.add(hop['id'])
        channels.add(hop['channel'])
        previous_amount, previous_delay = hop['amount_msat'], hop['delay']
    if (route[-1]['id'] != policy['destination'] or route[-1]['amount_msat'] != amount_msat
            or route[-1]['delay'] != policy['final_cltv']):
        raise ValueError('reverse route final hop differs from invoice policy')
    fee = route[0]['amount_msat'] - amount_msat
    if fee > policy['max_fee_msat']:
        raise ValueError('reverse route exceeds fee budget')
    return fee


def convert(result, amount_msat, policy, *, _prefix=False):
    limits(policy, _prefix=_prefix)
    routes = result['routes']
    if len(routes) != 1:
        raise ValueError('reverse fixture requires exactly one route')
    chosen = routes[0]
    if chosen['amount_msat'] != amount_msat or chosen['final_cltv'] != policy['final_cltv']:
        raise ValueError('route planner changed requested amount or final CLTV')
    hops = chosen['path']
    if not 1 <= len(hops) <= policy['max_hops']:
        raise ValueError('route planner returned too many hops or none')
    route = []
    previous = None
    for hop in hops:
        channel, direction = hop['short_channel_id_dir'].rsplit('/', 1)
        if direction not in ('0', '1') or int(direction) != int(hop['node_id_in'] > hop['node_id_out']):
            raise ValueError('route planner channel direction mismatch')
        for key in ('amount_in_msat', 'amount_out_msat', 'cltv_in', 'cltv_out'):
            if type(hop[key]) is not int:
                raise ValueError('route planner returned noninteger amounts or delays')
        if hop['amount_in_msat'] < hop['amount_out_msat'] or hop['cltv_in'] < hop['cltv_out']:
            raise ValueError('route planner returned negative hop fees or delay')
        if previous is None:
            if (hop['node_id_in'] != policy['source'] or hop['amount_in_msat'] != hop['amount_out_msat']
                    or hop['cltv_in'] != hop['cltv_out']):
                raise ValueError('route planner did not return a source-free route')
        elif (hop['node_id_in'] != previous['node_id_out']
              or hop['amount_in_msat'] != previous['amount_out_msat']
              or hop['cltv_in'] != previous['cltv_out']):
            raise ValueError('route planner returned a discontinuous path')
        route.append(dict(id=hop['node_id_out'], channel=channel,
                          amount_msat=hop['amount_out_msat'], delay=hop['cltv_out']))
        previous = hop
    validate(route, amount_msat, policy, _prefix=_prefix)
    return route


def query(cli, amount, policy, rpc):
    return rpc([*cli, '-k'], 'getroutes', 'source='+policy['source'],
               'destination='+policy['destination'], 'amount_msat='+str(amount),
               'layers=["auto.localchans","auto.sourcefree"]',
               'maxfee_msat='+str(policy['max_fee_msat']),
               'final_cltv='+str(policy['final_cltv']),
               'maxdelay='+str(policy['max_delay']), 'maxparts=1')


def no_route(error):
    # Only the pinned CLN PAY_ROUTE_NOT_FOUND error permits another candidate.
    # Transport errors, timeouts and other RPC failures must not be hidden.
    try:
        reply = json.loads(error.stdout)
        return isinstance(reply, dict) and reply.get('code') == 205
    except (ValueError, TypeError):
        return False


def hinted_tail(hint, amount, policy):
    if not isinstance(hint, list) or not 1 <= len(hint) <= policy['max_hops']:
        raise ValueError('invalid reverse invoice hint length')
    required = {'pubkey', 'short_channel_id', 'fee_base_msat',
                'fee_proportional_millionths', 'cltv_expiry_delta'}
    nodes, channels = {policy['destination']}, set()
    for index, hop in enumerate(hint):
        if not isinstance(hop, dict) or set(hop) != required:
            raise ValueError('invalid reverse invoice hint fields')
        node, channel = hop['pubkey'], hop['short_channel_id']
        if (not isinstance(node, str) or not re.fullmatch(r'0[23][0-9a-f]{64}', node)
                or node in nodes or (node == policy['source'] and index != 0)
                or not isinstance(channel, str) or not re.fullmatch(r'[0-9]+x[0-9]+x[0-9]+', channel)
                or channel in channels):
            raise ValueError('invalid or looping reverse invoice hint')
        if any(n > bound for n, bound in zip(map(int, channel.split('x')), (0xffffff, 0xffffff, 0xffff))):
            raise ValueError('reverse hint channel identifier out of range')
        for key, maximum in (('fee_base_msat', 0xffffffff),
                             ('fee_proportional_millionths', 0xffffffff),
                             ('cltv_expiry_delta', 0xffff)):
            if type(hop[key]) is not int or not 0 <= hop[key] <= maximum:
                raise ValueError('invalid reverse hint fee or delta')
        nodes.add(node)
        channels.add(channel)
    destination, delay, tail = policy['destination'], policy['final_cltv'], []
    for hop in reversed(hint):
        tail.insert(0, dict(id=destination, channel=hop['short_channel_id'],
                            amount_msat=amount, delay=delay))
        # The sender does not charge itself a forwarding fee or CLTV delta.
        if hop['pubkey'] != policy['source']:
            amount += hop['fee_base_msat'] + amount * hop['fee_proportional_millionths'] // 1000000
            delay += hop['cltv_expiry_delta']
        destination = hop['pubkey']
    return tail, destination, amount, delay


def plan(cli, decoded, source, rpc, max_fee_msat=10000, max_delay=80, max_hops=4):
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != 'bcrt' or not decoded.get('payment_secret')
            or type(decoded.get('amount_msat')) is not int or decoded['amount_msat'] != 100000000
            or type(decoded.get('min_final_cltv_expiry')) is not int
            or decoded['min_final_cltv_expiry'] < 0):
        raise ValueError('reverse route planning requires a signed BTC regtest fixture invoice')
    amount = decoded['amount_msat']
    policy = dict(source=source, destination=decoded['payee'], max_fee_msat=max_fee_msat,
                  max_delay=max_delay, max_hops=max_hops,
                  final_cltv=max(40, decoded['min_final_cltv_expiry']))
    limits(policy)
    hints = decoded.get('routes', [])
    if not isinstance(hints, list) or len(hints) > 8:
        raise ValueError('too many or malformed reverse invoice hints')
    try:
        result = query(cli, amount, policy, rpc)
    except subprocess.CalledProcessError as error:
        if not no_route(error):
            raise
        unavailable = error
    else:
        return convert(result, amount, policy), policy
    # Hints are alternative tails, not a reason to mutate public gossip or
    # create shared askrene layers. Try at most eight; choose the first fit.
    for hint in hints:
        try:
            tail, entry, entry_amount, entry_delay = hinted_tail(hint, amount, policy)
            fee = entry_amount - amount
            if fee > max_fee_msat or entry_delay > max_delay:
                continue
            prefix_policy = dict(policy, destination=entry, max_fee_msat=max_fee_msat-fee,
                                 final_cltv=entry_delay, max_hops=max_hops-len(tail))
            if entry != source:
                limits(prefix_policy, _prefix=True)
        except ValueError:
            continue
        prefix = []
        if entry != source:
            try:
                result = query(cli, entry_amount, prefix_policy, rpc)
            except subprocess.CalledProcessError as error:
                if not no_route(error):
                    raise
                continue
            prefix = convert(result, entry_amount, prefix_policy, _prefix=True)
        route = prefix + tail
        try:
            validate(route, amount, policy)
        except ValueError:
            continue  # A public prefix can intersect the private tail.
        return route, policy
    raise unavailable
