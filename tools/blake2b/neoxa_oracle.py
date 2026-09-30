#!/usr/bin/env python3
"""Read-only BTCB2/BTC replacement-cost estimate. Never invokes Lightning RPCs."""
import argparse
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import json
import time
import urllib.request

BASE = 'https://neoxa.exchange/api/exchange/'
PAIR = 'BTCB2_BTC'
D = Decimal
SAT = D(100000000)
BPS = D(10000)


def number(value):
    if isinstance(value, bool):
        raise ValueError('invalid numeric value')
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid numeric value') from None
    if not result.is_finite() or result <= 0 or result > D('1e18') or result < D('1e-18'):
        raise ValueError('numeric value out of bounds')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('oracle redirect refused')


def fetch(kind):
    if kind not in ('ticker', 'orderbook'):
        raise ValueError('unsupported endpoint')
    url = BASE + kind + '/' + PAIR
    request = urllib.request.Request(url, headers={'User-Agent': 'cln-xbt-oracle/1.0',
                                                 'Accept': 'application/json',
                                                 'Cache-Control': 'no-cache'})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=10) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('oracle response too large')
    return json.loads(raw, parse_float=D)


def estimate(ticker, book, xbt_sats, *, now_ms, margin_bps=0,
             max_age_seconds=30, max_spread_bps=500, max_slippage_bps=100,
             min_price=None, max_price=None):
    if type(xbt_sats) is not int or not 0 < xbt_sats <= 21000000 * 100000000:
        raise ValueError('invalid XBT amount')
    for value in (margin_bps, max_spread_bps, max_slippage_bps):
        if type(value) is not int or not 0 <= value <= 10000:
            raise ValueError('basis points must be integers from 0 to 10000')
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300:
        raise ValueError('invalid maximum data age')
    for data in (ticker, book):
        if data.get('success') is not True or data.get('pair') != PAIR:
            raise ValueError('oracle market mismatch')
    tick = ticker['ticker']
    timestamp = tick['computedAt']
    if type(timestamp) is not int or not 0 <= now_ms - timestamp <= max_age_seconds * 1000:
        raise ValueError('stale or future ticker timestamp')
    with localcontext() as context:
        context.prec = 60
        bid, ask = number(tick['bestBid']), number(tick['bestAsk'])
        if ask < bid or (ask-bid)/bid*BPS > max_spread_bps:
            raise ValueError('crossed or excessive market spread')
        levels = []
        # AMM levels are indicative samples, not independent limit orders.
        # Do not sum them as if their quantities were executable liquidity.
        for level in book['asks']:
            if level.get('isAmm') is True:
                continue
            if level.get('isAmm', False) is not False:
                raise ValueError('invalid liquidity type')
            levels.append((number(level['price']), number(level['quantity'])))
        if not levels:
            raise ValueError('no limit-order ask liquidity')
        levels.sort()
        if levels[0][0] < bid:
            raise ValueError('inconsistent ticker and order book')
        remaining = D(xbt_sats) / SAT
        cost = D(0)
        fills = []
        for price, quantity in levels:
            take = min(remaining, quantity)
            cost += take * price
            fills.append({'price_btc_per_xbt': str(price), 'xbt': str(take)})
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise ValueError('insufficient limit-order depth')
        average = cost / (D(xbt_sats) / SAT)
        if (average/ask-1)*BPS > max_slippage_bps:
            raise ValueError('replacement cost exceeds slippage limit')
        if min_price is not None and average < number(min_price):
            raise ValueError('price below operator minimum')
        if max_price is not None and average > number(max_price):
            raise ValueError('price above operator maximum')
        btc_sats = int((cost * SAT * (1+D(margin_bps)/BPS)).to_integral_value(rounding=ROUND_CEILING))
        return dict(source=BASE, pair=PAIR, mode='limit-order-asks',
                    ticker_computed_at_ms=timestamp, checked_at_ms=now_ms,
                    xbt_sats=xbt_sats, btc_sats=btc_sats, margin_bps=margin_bps,
                    average_btc_per_xbt=str(average), fills=fills,
                    policy=dict(max_age_seconds=max_age_seconds,
                                max_spread_bps=max_spread_bps, max_slippage_bps=max_slippage_bps,
                                min_price=min_price, max_price=max_price),
                    read_only=True, exchange_fees_included=False,
                    lightning_fees_included=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xbt-sats', type=int, required=True)
    parser.add_argument('--margin-bps', type=int, default=0)
    parser.add_argument('--max-age-seconds', type=int, default=30)
    parser.add_argument('--max-spread-bps', type=int, default=500)
    parser.add_argument('--max-slippage-bps', type=int, default=100)
    parser.add_argument('--min-price')
    parser.add_argument('--max-price')
    args = parser.parse_args()
    try:
        start = time.monotonic()
        ticker = fetch('ticker')
        book = fetch('orderbook')
        if time.monotonic()-start > 15:
            raise ValueError('oracle snapshot acquisition too slow')
        result = estimate(ticker, book, now_ms=time.time_ns()//1000000, **vars(args))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # No cached-price fallback. Do not expose response bodies or endpoints in errors.
        print(json.dumps({'event': 'oracle_unavailable',
                          'reason': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                          else 'market data could not be fetched or validated'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
