"""Read-only audit of advertised policies for remote BTC route hops.

BOLT11 private hints do not advertise HTLC minima or maxima. Missing gossip
is reported as unknown, never filled in from another direction or channel.
This does not establish actual remote liquidity or guarantee forwarding.
The caller must first validate route shape, continuity and global bounds.
"""


def inspect_remote_policies(route, lookup):
    checked = unknown = 0
    violations = set()
    for previous, hop in zip(route, route[1:]):
        reply = lookup(hop['channel'])
        if not isinstance(reply, dict) or not isinstance(reply.get('channels'), list):
            raise ValueError('invalid remote channel policy response')
        rows = reply['channels']
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError('invalid remote channel policy response')
        matches = [row for row in rows
                   if row.get('short_channel_id') == hop['channel']
                   and row.get('source') == previous['id']
                   and row.get('destination') == hop['id']]
        if not matches:
            unknown += 1
            continue
        if len(matches) != 1:
            raise ValueError('ambiguous remote channel policy')
        row = matches[0]
        fields = ('htlc_minimum_msat', 'htlc_maximum_msat',
                  'base_fee_millisatoshi', 'fee_per_millionth', 'delay')
        if (any(type(row.get(k)) is not int or row[k] < 0 for k in fields)
                or row['htlc_minimum_msat'] > row['htlc_maximum_msat']
                or type(row.get('active')) is not bool
                or type(row.get('direction')) is not int
                or row['direction'] != int(previous['id'] > hop['id'])):
            raise ValueError('invalid remote channel policy fields')
        checked += 1
        if not row['active']:
            violations.add('remote hop advertised disabled')
        amount = hop['amount_msat']
        if amount < row['htlc_minimum_msat']:
            violations.add('remote hop amount below advertised HTLC minimum')
        if amount > row['htlc_maximum_msat']:
            violations.add('remote hop amount above advertised HTLC maximum')
        fee = row['base_fee_millisatoshi'] + amount * row['fee_per_millionth'] // 1000000
        if previous['amount_msat'] - amount < fee:
            violations.add('remote hop fee below current advertised requirement')
        if previous['delay'] - hop['delay'] < row['delay']:
            violations.add('remote hop delay below current advertised requirement')
    return dict(remote_btc_policy_hops_checked=checked,
                remote_btc_policy_hops_unknown=unknown,
                remote_btc_htlc_minima_checked=unknown == 0,
                remote_btc_htlc_limits_passed=unknown == 0 and not violations,
                remote_btc_policy_violations=sorted(violations))
