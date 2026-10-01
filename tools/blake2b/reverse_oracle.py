#!/usr/bin/env python3
"""Read-only XBT cost for a BTC payment plus a maximum routing-fee allowance."""
import argparse
from decimal import Decimal, localcontext
from fractions import Fraction
import json
import time

from neoxa_oracle import BASE, PAIR, fetch, number

SUPPLY_SATS = 21000000 * 100000000


def fraction(value):
    return Fraction(number(value))


def display(value):
    # Arithmetic and comparisons use exact fractions. Decimal strings are
    # display-only, including a possibly repeating average exchange rate.
    with localcontext() as context:
        context.prec = 60
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def estimate(ticker, book, btc_sats, *, max_routing_fee_sats, now_ms,
             margin_bps=0, max_age_seconds=30, max_spread_bps=500,
             max_slippage_bps=100, min_price=None, max_price=None):
    if type(btc_sats) is not int or not 0 < btc_sats <= SUPPLY_SATS:
        raise ValueError('invalid BTC amount')
    if (type(max_routing_fee_sats) is not int or not 0 <= max_routing_fee_sats <= SUPPLY_SATS
            or btc_sats + max_routing_fee_sats > SUPPLY_SATS):
        raise ValueError('invalid BTC routing-fee allowance')
    for value in (margin_bps, max_spread_bps, max_slippage_bps):
        if type(value) is not int or not 0 <= value <= 10000:
            raise ValueError('basis points must be integers from 0 to 10000')
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise ValueError('invalid maximum data age')
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError('invalid snapshot check time')
    for data in (ticker, book):
        if data.get('success') is not True or data.get('pair') != PAIR:
            raise ValueError('oracle market mismatch')
    tick = ticker['ticker']
    timestamp = tick['computedAt']
    if type(timestamp) is not int or not 0 <= now_ms - timestamp <= max_age_seconds * 1000:
        raise ValueError('stale or future ticker timestamp')
    bid, ask = fraction(tick['bestBid']), fraction(tick['bestAsk'])
    if ask < bid or (ask-bid)*10000 > bid*max_spread_bps:
        raise ValueError('crossed or excessive market spread')
    lower = fraction(min_price) if min_price is not None else None
    upper = fraction(max_price) if max_price is not None else None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError('operator price bounds reversed')
    levels = []
    for level in book['bids']:
        if level.get('isAmm') is True:
            continue
        if level.get('isAmm', False) is not False:
            raise ValueError('invalid liquidity type')
        price = fraction(level['price'])
        # Never count a fraction of an XBT sat as spendable order depth.
        quantity_sats = fraction(level['quantity']) * 100000000
        capacity = quantity_sats.numerator // quantity_sats.denominator
        if capacity:
            levels.append((price, capacity))
    if not levels:
        raise ValueError('no limit-order bid liquidity')
    levels.sort(reverse=True)
    if levels[0][0] > ask:
        raise ValueError('inconsistent ticker and order book')
    budget = btc_sats + max_routing_fee_sats
    target = Fraction(budget * (10000+margin_bps), 10000)
    proceeds, xbt_sats, fills = Fraction(0), 0, []
    for price, capacity in levels:
        needed = (target-proceeds) / price
        take = min(capacity, (needed.numerator+needed.denominator-1)//needed.denominator)
        proceeds += take * price
        xbt_sats += take
        fills.append(dict(price_btc_per_xbt=display(price), xbt_sats=take,
                          xbt=display(Fraction(take, 100000000))))
        if proceeds >= target:
            break
    if proceeds < target:
        raise ValueError('insufficient limit-order bid depth')
    if xbt_sats > SUPPLY_SATS:
        raise ValueError('required XBT exceeds supply bound')
    average = proceeds / xbt_sats
    if (bid-average)*10000 > bid*max_slippage_bps:
        raise ValueError('bid proceeds fall below slippage limit')
    if lower is not None and average < lower:
        raise ValueError('price below operator minimum')
    if upper is not None and average > upper:
        raise ValueError('price above operator maximum')
    return dict(source=BASE, pair=PAIR, direction='xbt-to-btc', mode='limit-order-bids',
                ticker_computed_at_ms=timestamp, checked_at_ms=now_ms,
                btc_sats=btc_sats, max_routing_fee_sats=max_routing_fee_sats,
                btc_budget_sats=budget, margin_bps=margin_bps,
                target_bid_proceeds_btc_sats=display(target), xbt_sats=xbt_sats,
                estimated_bid_proceeds_btc_sats=display(proceeds),
                average_btc_per_xbt=display(average), fills=fills,
                policy=dict(max_age_seconds=max_age_seconds, max_spread_bps=max_spread_bps,
                            max_slippage_bps=max_slippage_bps, min_price=min_price, max_price=max_price),
                read_only=True, exchange_fees_included=False,
                routing_fee_allowance_included=True, route_checked=False,
                channel_capacity_checked=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--btc-sats', required=True, type=int)
    parser.add_argument('--max-routing-fee-sats', required=True, type=int)
    parser.add_argument('--margin-bps', type=int, default=0)
    parser.add_argument('--max-age-seconds', type=int, default=30)
    parser.add_argument('--max-spread-bps', type=int, default=500)
    parser.add_argument('--max-slippage-bps', type=int, default=100)
    parser.add_argument('--min-price')
    parser.add_argument('--max-price')
    args = parser.parse_args()
    try:
        start = time.monotonic()
        ticker, book = fetch('ticker'), fetch('orderbook')
        if time.monotonic()-start > 15:
            raise ValueError('oracle snapshot acquisition too slow')
        print(json.dumps(estimate(ticker, book, now_ms=time.time_ns()//1000000, **vars(args))))
        return 0
    except Exception as exc:
        print(json.dumps(dict(event='oracle_unavailable', reason=str(exc)
                              if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                              else 'market data could not be fetched or validated')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
