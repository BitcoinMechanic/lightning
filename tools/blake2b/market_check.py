"""Read-only market/HTLC feasibility check for the established direct channels."""
import json
import time

import live_pilot as pilot
from neoxa_oracle import estimate, fetch
from swap_rpc import RPC


def minimum_sats(channel):
    fee, dust = channel['feerate']['perkw'], channel['dust_limit_msat']
    if type(fee) is not int or type(dust) is not int or fee <= 0 or dust < 0:
        raise ValueError('invalid channel fee data')
    return (dust + ((703 * fee + 999) // 1000) * 1000) // 1000 + 1


def capacity(btc, xbt, ticker, book, *, now_ms, margin_bps):
    """Whole-sat interval where market pricing fits both untrimmed HTLCs."""
    for channel in (btc, xbt):
        if (channel['state'] != 'CHANNELD_NORMAL' or channel.get('peer_connected') is not True
                or channel.get('htlcs')):
            raise ValueError('both channels must be connected, normal and have no pending HTLCs')
    receivable, spendable = btc['receivable_msat'], xbt['spendable_msat']
    if any(type(v) is not int or v < 0 for v in (receivable, spendable)):
        raise ValueError('invalid channel capacity')
    btc_min, xbt_min = minimum_sats(btc), minimum_sats(xbt)
    maximum = spendable // 1000
    btc_max = receivable // 1000
    result = dict(read_only=True, btc_minimum_sats=btc_min, btc_receivable_sats=btc_max,
                  xbt_minimum_sats=xbt_min, xbt_spendable_sats=maximum,
                  margin_bps=margin_bps, feasible=False)
    if maximum < xbt_min or btc_max < btc_min:
        return dict(result, reason='channel balance below HTLC minimum')

    def price(amount):
        return estimate(ticker, book, amount, now_ms=now_ms, margin_bps=margin_bps)

    top = price(maximum)
    result.update(btc_sats_at_full_xbt_balance=top['btc_sats'],
                  ticker_computed_at_ms=top['ticker_computed_at_ms'], checked_at_ms=now_ms)
    if top['btc_sats'] < btc_min:
        return dict(result, reason='XBT spendable balance cannot cover BTC HTLC minimum at market price')
    # Cost is monotone for positive, sorted ask levels. Search whole sats,
    # using the same frozen snapshot and rounding as actual oracle estimates.
    lo, hi = xbt_min, maximum
    while lo < hi:
        mid = (lo+hi)//2
        if price(mid)['btc_sats'] >= btc_min:
            hi = mid
        else:
            lo = mid+1
    lower = lo
    if price(lower)['btc_sats'] > btc_max:
        return dict(result, reason='BTC receivable balance below first eligible market amount')
    lo, hi = lower, maximum
    while lo < hi:
        mid = (lo+hi+1)//2
        if price(mid)['btc_sats'] <= btc_max:
            lo = mid
        else:
            hi = mid-1
    return dict(result, feasible=True, minimum_xbt_sats=lower, maximum_xbt_sats=lo,
                btc_sats_at_minimum=price(lower)['btc_sats'],
                btc_sats_at_maximum=price(lo)['btc_sats'])


def check(directory, margin_bps):
    # Historical quote supplies private channel and identity bindings only;
    # its expired terms and completed controller state are never modified.
    data = json.loads((directory / 'quote.json').read_text())
    config = data['config']
    if not pilot.is_live(config):
        raise ValueError('market check requires established live operator configuration')
    pilot.verify_nodes(dict(config, node_ids=data['node_ids']), RPC.call)
    pilot.require_reserves(config, RPC.call)
    btc_id = data['terms'].get('btc_channel')
    route = data['controller']['route']
    if not btc_id or len(route) != 1:
        raise ValueError('quote must identify both original direct channels')
    btc = [c for c in RPC.call(config['btc_cli'], 'listpeerchannels')['channels']
           if c.get('short_channel_id') == btc_id]
    xbt = [c for c in RPC.call(config['xbt_cli'], 'listpeerchannels')['channels']
           if c.get('short_channel_id') == route[0]['channel'] and c['peer_id'] == route[0]['id']]
    if len(btc) != 1 or len(xbt) != 1:
        raise ValueError('original direct channels not found')
    start = time.monotonic()
    ticker, book = fetch('ticker'), fetch('orderbook')
    if time.monotonic()-start > 15:
        raise ValueError('oracle snapshot acquisition too slow')
    return capacity(btc[0], xbt[0], ticker, book, now_ms=time.time_ns()//1000000,
                    margin_bps=margin_bps)
