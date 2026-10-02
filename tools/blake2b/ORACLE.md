# Neoxa read-only price reference

Run from the repository with the project's venv:

```
.venv/bin/python tools/blake2b/neoxa_oracle.py --xbt-sats 500000 --margin-bps 100
```

This requests an estimate for 500,000 XBT sats, with a 1% operator margin.
It does not create an invoice, register a quote, call Lightning, place an
exchange order, or change existing swaps. No API credentials are needed.

Public API documentation: https://neoxa.exchange/api-docs
Endpoints: `/api/exchange/ticker/BTCB2_BTC` and
`/api/exchange/orderbook/BTCB2_BTC`. Neoxa's ledger symbol BTCB2 maps to XBT.
Prices are BTC per whole XBT; both currencies have 100,000,000 sats per coin.

The estimate sums ordinary ask-side limit-order depth for the requested XBT,
adds the specified margin, and rounds BTC upward to a whole satoshi using
Decimal arithmetic. AMM levels (`isAmm: true`) are excluded: their displayed
quantities are not treated as independently executable limit orders. The
result is an indicative replacement cost, not an executable exchange quote.
Exchange trading/withdrawal fees and Lightning fees are not included.

Defaults: ticker at most 30 seconds old, no future timestamps, spread at most
500 basis points (5%), and average cost at most 100 basis points above the
reported best ask. Override with `--max-age-seconds`, `--max-spread-bps` and
`--max-slippage-bps`; optional `--min-price`/`--max-price` bound BTC per XBT.
The book has no source timestamp in the observed API response; the reader
bounds acquisition time but cannot prove the exchange's book is fresh.
Two REST responses are not an atomic snapshot. Inconsistent data can cause
refusal and should be fetched anew rather than relaxing limits blindly.

HTTPS certificate verification remains enabled; redirects are refused.
Standard HTTPS proxy environment settings are supported. There is no cached
price fallback. Malformed, stale, crossed, or insufficient data causes a
nonzero exit with `oracle_unavailable`. Output contains timestamps, fills,
amounts and policy for later recording alongside an immutable swap quote.

The fixed live pilot profiles retain their original limits. For variable
amounts, use the separate explicitly enabled profile in MARKET-SWAPS.md; never
substitute estimated amounts into an existing pilot state file. API availability is not a guarantee of
price integrity: this remains a single exchange price source.

## Check the established channels before creating a market quote

```
.venv/bin/python tools/blake2b/swap_service.py market-check \
  --directory "$HOME/cln-live-pilot/swap-2" --margin-bps 100
```

This read-only command takes the operator identities and exact direct-channel
bindings from an existing v2 quote. It does not reuse its price or alter its
quote, state, gate, invoices or locks. It requires normal, connected channels
without pending HTLCs, matching live operator identities, and the existing
50,000-sat confirmed unreserved reserve on each operator.

The output reports BTC receivable balance, XBT spendable balance and the
conservative current-fee untrimmed minimum on each side. If even all spendable
XBT prices below the BTC minimum, `feasible` is false. Otherwise it finds the
whole-sat XBT interval whose rounded market BTC charge fits the incoming
balance and both fee minimums. No private channel IDs, hashes or invoices are
printed. A false feasibility result is a successful diagnostic (exit zero);
a failed data/RPC check is a nonzero error. Market depth/slippage is checked
for the entire spendable XBT balance; refusal at that size does not prove
that no smaller swap could fit. This is a conservative readiness check.

The result is momentary, not a reservation, signed invoice, or authorization
to spend. The separate market profile requires explicit enablement (MARKET-SWAPS.md);
the fixed pilot profiles still enforce their original amounts. Funding and exchange price
changes can invalidate any reported range.


## Reverse XBT-to-BTC read-only pricing

```sh
.venv/bin/python tools/blake2b/reverse_oracle.py \
  --btc-sats 1500 --max-routing-fee-sats 10 --margin-bps 100
```

This prices receipt of XBT in exchange for a 1,500-sat BTC payment, with a
10-sat maximum BTC routing-fee allowance and 1% operator markup on their sum.
It takes an amount only: no private invoice, credentials or node identifiers
are needed. It performs no Lightning RPCs, creates no invoice or swap, changes
no service configuration, and places no exchange order.

Unlike the forward replacement-cost estimate, reverse pricing uses ordinary
**bids**: indicative BTC proceeds from selling the received XBT. The target is
`(btc_sats + max_routing_fee_sats) * (1 + margin_bps / 10000)` BTC sats. Starting
at the best limit bid, it finds enough whole XBT sats to cover that target.
Exact rational arithmetic avoids float/rounding shortfalls. Available depth is
rounded down to whole XBT sats; the final required fill rounds up. AMM samples
are excluded. The full routing allowance is priced, even when an eventual route
might cost less. This reader does not determine or enforce an actual route.

The output distinguishes the receiver's BTC amount, maximum routing fee, BTC
budget, marked-up target proceeds, required XBT, and estimated bid proceeds.
It includes fills, timestamps and the average BTC-per-XBT bid price. Exchange
trading and withdrawal fees remain excluded. This is indicative book pricing,
not guaranteed executable proceeds or a liquidity reservation.

Reverse quotes retain the 30-second ticker age and 5% spread defaults.
Depth slippage is capped at 1%, measured from the best usable **ordinary limit
bid** to the weighted average fill price. AMM samples remain excluded from all
fill quantities. Separately, the absolute gap between that best limit bid and
the ticker best bid is capped at 2% of the ticker bid. This intentionally permits
more ticker-to-limit divergence than the previous combined 1% check. At both
adverse boundaries, the average can be 2.98% below the ticker bid.

The standalone reader exposes `--max-reference-gap-bps` (default 200); service
quotes use that default, alongside the unchanged 100-bps depth-slippage cap.
Audit output includes `ticker_best_bid_btc_per_xbt`,
`best_limit_bid_btc_per_xbt`, `reference_gap_bps`, `depth_slippage_bps` and both
policy caps. A reference-gap refusal has API code `market_reference_gap`.
This change affects reverse quotes only; forward ask pricing is unchanged.
Existing saved quotes and uncertain request journals are not rewritten.
Optional BTC-per-XBT price bounds remain available. The book itself lacks a
source timestamp; two REST responses are not an atomic snapshot. HTTPS and
redirect protections are inherited from the forward oracle reader. There is
no cached-price fallback when data fails validation.

This does not establish live swap feasibility: the route, channel capacities,
untrimmed HTLC minimums and live timing policy are not checked. Neither the
forward market profile nor the regtest-only reverse controller is modified.
