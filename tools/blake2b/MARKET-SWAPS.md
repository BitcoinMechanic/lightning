# Bounded oracle-priced live swaps

`live-market-v1` is an experimental variable-amount profile layered on the
existing direct-channel swap controller. It uses Neoxa BTCB2/BTC ordinary
ask depth, a configured margin, and whole-satoshi rounding. It never trades
on the exchange. Exchange and Lightning fees are not included by the oracle.

Hard caps are 10,000 BTC sats and 500,000 XBT sats per swap; the configuration
must specify caps no higher than these and a margin of 0..500 basis points.
The first suggested configuration limits BTC to 3,000 sats and XBT to 400,000
sats with a 100-basis-point margin. These are per-swap limits, not lifetime or
on-chain fee limits. The receiver and both direct channels are explicitly pinned.

## Enable and configure

Restart the BTC launcher in its existing RPC environment, adding
`--market-swaps`. This persists a `market-swaps-v1` marker under the BTC node
root and reloads the existing gate and its quote database on later restarts.
Old pilot records and held-hook recovery remain supported. The gate accepts
new market quotes only with this explicit opt-in. It still passes unrelated
ordinary invoices through.

```
.venv/bin/python tools/blake2b/live_btc_node.py \
  --lightning-dir="$HOME/cln-btc-observe" \
  --listen-host="$BTC_LN_HOST" --listen-port=19735 --market-swaps
```

Create a new configuration using the completed v2 pilot as the source of
operator identities, BTC channel and receiver. The setup selects the one
currently normal XBT channel to that receiver, so closed historical channels
are preserved. It requires connected channels without pending HTLCs and
50,000 confirmed unreserved sats on each operator. It makes no payment RPCs.

```
.venv/bin/python tools/blake2b/market_setup.py \
  --previous-directory "$HOME/cln-live-pilot/swap-2" \
  --config "$HOME/cln-live-pilot/operators-market.json" \
  --max-btc-sats 3000 --max-xbt-sats 400000 --margin-bps 100
```

Configuration files are mode 0600. Repeating identical setup is harmless;
a differing existing configuration is not overwritten. Do not edit completed
quote directories or migrate controller state into new directories.

## Quote and pay

Create a fresh fixed-amount XBT BOLT11 invoice at the configured receiver,
with enough remaining lifetime. For example, 350,000 sats is 350,000,000 msat.
Keep the signed invoice in a private local file. Then use a fresh swap directory:

```
.venv/bin/python tools/blake2b/swap_service.py quote-market \
  --config "$HOME/cln-live-pilot/operators-market.json" \
  --directory "$HOME/cln-live-pilot/market-1" \
  --xbt-invoice "$(cat "$HOME/cln-live-pilot/market-xbt-invoice.txt")"
```

The response contains a private BTC invoice; do not post the raw response.
This command does not submit an XBT payment. It checks the signed XBT invoice,
node identities, both channel balances, current untrimmed HTLC minima, reserve
funds, and absence of an earlier outgoing attempt. It fetches a fresh oracle
snapshot and records the calculated price, timestamp, depth fills and margin.
Publication rejects source data older than 30 seconds and fixes the BTC
invoice lifetime to at most two minutes (also bounded by the XBT expiry).
There is no automatic renewal of a market quote.

Start the service before paying the BTC invoice:

```
.venv/bin/python tools/blake2b/swap_service.py run \
  --directory "$HOME/cln-live-pilot/market-1"
```

The controller rechecks reserves, fees, exact incoming HTLC and pinned outgoing
route before spending. It never reprices an issued invoice or recovery attempt.
A digest of the price record and a unique controller identity bind local state
to the gate quote. Creating the same payment hash in another directory gets a
new controller identity and cannot replace the existing registration. Always
recover using the original canonical directory; never copy or reset state.

The gate admits one active quote at a time. A second distinct quote is refused
while a prior quote is held or unexpired. Expired never-accepted quotes and
terminal records remain stored. Registration is idempotent for identical terms.
An expired held quote is never treated as unused. Payment recovery uses the
existing single-attempt, preimage, definite-failure and BTC-deadline handling.

If publication is interrupted, `invoice --directory ...` retries the saved
terms without refetching prices, provided their freshness checks still pass.
If the source snapshot becomes too old, retain the directory and let the
quote expire. An unused expired quote can be replaced with a newly priced
quote using a fresh receiver invoice and a fresh directory; never do this to
recover an accepted payment. Registered hashes remain immutable even after
expiry, so a new directory alone cannot reprice the old receiver invoice.
Pre-spend refusal can leave BTC held and requires the existing deliberate
inspection/cancellation workflow. It does not silently spend or fail BTC.

## Validation and limits

`test_market_quotes.py` exercises immutable price success/pending recovery,
no duplicate send, state tampering, stale publication, caps, gate serialization
across process restarts, and private configuration setup with mocked CLN RPCs.
The existing gate protocol is tested in real Python subprocesses. This is not
a new daemon-backed market-profile end-to-end test or a production audit.

The source is a single exchange. Ask-depth estimates can disappear before
execution, AMM levels are deliberately excluded, and the book lacks a source
timestamp. Quotes freeze a price for their short lifetime; they are not hedged.
The existing 288-block admission minimum, 300-block invoice CLTV and 72-block
BTC close threshold remain experimental cross-chain timing policy. Keep both
operators and the watcher running. Confirm payer completion and receiver paid
status; `btc_released` records intent, not final peer settlement.

## Foreground receiving command

`receive_workflow.py receive` combines receiver invoice creation, market quote
publication, BTC invoice export, watcher startup and receiver receipt checking:

```
.venv/bin/python tools/blake2b/receive_workflow.py receive \
  --config "$HOME/cln-live-pilot/operators-market.json" \
  --receiver-dir "$HOME/cln-xbt-peer" \
  --directory "$HOME/cln-live-pilot/receive-1" --xbt-sats 350000
```

Keep the process running. When it prints `invoice_ready`, copy the invoice
locally from a second terminal:

```
cat "$HOME/cln-live-pilot/receive-1/btc-invoice.txt"
```

The quote retains its two-minute expiry. This command does not pay BTC for
you. Its output omits hashes, secrets, node IDs and invoice text; repeated
identical waiting messages are suppressed. On completion it checks the
receiver invoice and reports `receiver_paid` and `received_xbt_sats`.
Continue to verify completion in the BTC payer wallet as well.

A durable `request.json` is written before the receiver invoice RPC, including
a unique label. Retrying after a lost reply recovers that label. Resume with
the exact same command, config, amount and directory: it reuses the invoice,
quote and controller state. Changing the amount/config or copying a request
to another directory is refused. The underlying service records live in the
`swap` subdirectory; old manual workflows and records are unaffected.
A stable lock serializes orchestration and the existing service lock serializes
controller startup. Never delete request/state files or retry in a new directory
when a payment may have been accepted. An unused expired quote needs deliberate
replacement, not automatic repricing. Invoice creation itself does not spend.

## Return XBT through the channel for testing

This explicitly requested operation returns the received XBT principal toward
the operator through the exact original channel, without exchanging currencies:

```
.venv/bin/python tools/blake2b/receive_workflow.py repay \
  --directory "$HOME/cln-live-pilot/market-1" \
  --receiver-dir "$HOME/cln-xbt-peer"
```

For swaps made with the new wrapper, use `receive-1/swap` as the directory.
The amount is capped at both the original receipt and the receiver's current
whole-satoshi spendable balance (maximum 500,000 sats). A channel reserve may
prevent returning the entire receipt. It creates an operator invoice and uses
one direct, zero-routing-fee sendpay attempt. Both XBT nodes must stay running.
No new channel funding or on-chain transaction is requested.

`repayment.json` journals the original identity, amount, label and send intent;
original quote/controller files are not changed. Repeating against the same
original directory reconciles that one attempt and never sends again. A crash
between saving intent and submitting can leave an unknown result; it refuses
an automatic retry. A pending result can be checked by repeating the same
command; a failure or missing attempt needs inspection. Never delete or copy
the record to force another send. Successful output verifies the operator's
paid invoice and reports the actual `returned_xbt_sats`.

Offline tests cover invoice-reply loss, workflow locking, private output,
immutable requests, automatic receipt checks, repayment reserve limits,
repeated completion, lost submission replies and ambiguous missing attempts.

## Background operation

See SERVICES.md for user systemd units, private credential capture and
reconciliation of already-started swaps after terminal closure or reboot.
Prepared/unsubmitted swaps deliberately require manual foreground resume.
