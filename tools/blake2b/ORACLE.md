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
