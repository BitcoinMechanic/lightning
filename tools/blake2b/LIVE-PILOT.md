# Replacement pilot (v2)

Patch 0048 preserves v1 recovery and adds one controlled replacement after the
1,000/2,000-sat quote was cancelled before spending. New profile `live-pilot-v2`
requires exactly **2,000 BTC sats -> 4,000 XBT sats**. Do not edit or remove the
old `swap-1` directory or gate database. The historical v1 instructions below
are retained for context; use a separate `operators-v2.json`, `swap-2`, and a
new receiver invoice label for the replacement.

The v2 configuration adds `"previous_state"` with the absolute path to the
original `swap-1/state.json`. All CLI arrays must match the original exactly.
Before creating or registering a new quote, the service locks and verifies the
old controller's `btc_failed` + `pre_spend_aborted` record, checks both operator
identities, confirms the original BTC gate binding is failed, and verifies no
original BTC HTLC or XBT attempt remains. Any XBT attempt, including a failed
attempt, disqualifies this replacement path.

The original BTC channel must be connected, normal, free of pending HTLCs,
and have enough receivable balance. Both channel fee thresholds are checked
before publication. The new quote is pinned to that BTC channel. The existing
pre-spend checks run again after BTC is held, because fees can change. Such a
refusal must still be handled as a no-spend cancellation, never bypassed.

The gate permits v2 registration only alongside the sole failed v1 quote with
the matching original channel. It retains both records, allows an exact
registration retry, and refuses a third quote. This is not a general quote
reset mechanism. No new v1 quote can be created by the launcher now enabling v2.

After applying the patch and passing `test_live_pilot.py`, stop the BTC node
cleanly and restart using the same launcher, environment and data directory.
The launcher installs the updated gate without overwriting its quotes database.
Check `lightning-cli ... xbt-pilot-info` reports `live-pilot-v2` and one saved
quote before creating the replacement. Existing v1 controller state remains
v1 and can still be reconciled.

All reserve, deadline, monitoring, and fee-risk qualifications below continue
to apply. The new amounts are a test ratio, not a market exchange rate.

---

# One-quote live BTC to XBT pilot

This is an explicitly enabled experiment for the operator's own nodes and
wallets. It is not a production exchange or an advertised swap service.
Regtest remains the default profile. The live profile permits exactly
1,000 BTC sats for exactly 2,000 XBT sats. This ratio is a test amount, not
a market quote. The BTC gate permits one registered payment hash for its
entire saved history, including after settlement, failure, or quote expiry.

Prerequisites: the BTC node has its private incoming channel from Zeus;
the XBT operator has a direct channel to the XBT receiver; each operator has
at least 50,000 confirmed unreserved on-chain sats on its own chain. No other
process should spend those reserves during the pilot. The service also checks
current channel fees and refuses amounts that may be trimmed from commitment
transactions. Refusal must not be bypassed by editing the state or limits.

## Validate before activation

Activate the project's venv. Apply this patch after 0045, then run:

```sh
python tools/blake2b/test_live_pilot.py -v
python tools/blake2b/regression.py --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --jobs 8
```

These tests do not touch the live nodes. Live-profile unit tests use mocks and
the actual gate subprocess protocol; the full daemon tests still use regtest.

## Enable the gate

Cleanly stop the BTC node with lightning-cli `stop`. In its launch terminal,
retain all five BTC_RPC_* exports, the venv, and the tower's BTC_LN_HOST value.
Restart the same data directory with:

```sh
python tools/blake2b/live_btc_node.py \
  --lightning-dir="$HOME/cln-btc-observe" \
  --listen-host="$BTC_LN_HOST" --listen-port=19735 --live-pilot
```

The launcher copies the gate into `cln-btc-observe/live-swap-gate.py`. Its durable
state is the adjacent `live-swap-gate.quotes.json`. Once installed, the launcher
loads this gate on subsequent starts even if the opt-in flag is omitted, so
an accidental flag omission cannot discard pending hooks. Keep using this
launcher for restarts. Do not unload the plugin or delete its state.
Unregistered payment hashes pass through normally, preserving ordinary invoices.

## Service configuration

Use one persistent private service directory, e.g. `$HOME/cln-live-pilot`,
outside the git repository. Create an `operators.json` file with:

```json
{
  "profile": "live-pilot-v1",
  "btc_cli": ["/absolute/repo/cli/lightning-cli", "--lightning-dir=/absolute/home/cln-btc-observe", "--network=bitcoin", "--json", "--notifications=none"],
  "xbt_cli": ["/absolute/repo/cli/lightning-cli", "--lightning-dir=/absolute/home/cln-xbt-observe", "--network=xbt", "--json", "--notifications=none"]
}
```

Replace the paths locally; JSON does not expand `$HOME` or `~`. File permissions
should be 0600 inside a 0700 directory. This file contains no backend RPC
credentials. The service uses local CLN sockets; only the BTC launcher needs
the backend's HTTPS environment variables.

Create one 2,000-sat XBT invoice on the receiver (`2000000msat`), with a unique
fixed label. If retrying, recover that invoice with `listinvoices`; do not
silently create another. Store the invoice locally. Then use the existing
`swap_service.py quote`, `run`, and `status` commands with the above config,
`--btc-sats 1000`, and one new swap subdirectory. The BTC invoice is signed by
the BTC operator, decoded and checked, and can be paid normally from Zeus.
Keep invoices and all service output private. Quote validity is at most ten
minutes, so do not publish it until ready to run the service and pay.

Both node IDs are captured in the quote and controller state. All controller
recovery invocations verify the same IDs and networks before changing payments.
Before XBT submission, the original BTC HTLC must be committed and locally
untrimmed, the XBT channel must be connected and spendable, and the funds,
signed invoice, quote, binding, and timelock checks must still pass.

## Deadline and recovery

The BTC invoice requests 300 blocks; admission and pre-spend require at least
288 BTC blocks remaining (maximum 2016). The direct XBT route uses 40 XBT
blocks. If XBT stays pending and the incoming BTC HTLC reaches 72 blocks
remaining, the running controller requests a unilateral close of the exact
bound BTC channel. This can incur on-chain fees and close the Zeus channel.
The stored target and existing recovery logic make close retries idempotent.

Independent chains can stall or reorganize: these block counts are an
experimental policy, not a guarantee of equal time or lossless swaps. Keep
both CLN nodes and the foreground service running and monitored. Channel
fees can exceed the swapped amounts, and the amount limits do not cap closing
fees. This pilot is for the user's own cooperative receiver, not arbitrary
untrusted receivers or concurrent customers.

On RPC timeout or pending XBT, restart `run` against the same canonical swap
directory. Never copy state to a new directory, delete the gate database, or
resubmit XBT manually. Missing or ambiguous outgoing results require inspection;
they never authorize a retry or BTC failure. A pre-spend refusal leaves BTC held
for inspection, and does not spend XBT. An expired quote cannot cancel a swap
already accepted by the gate. A paid XBT result yields the preimage used to
release BTC. Verify Zeus reports complete and the XBT receiver invoice reports
paid; controller phase `btc_released` alone records release intent, not final
peer settlement. Do not close the channels with pending HTLCs manually.

## One renewal of an unused v2 quote

`swap_service.py renew --directory PATH` renews an expired, published v2 quote
once, using the same hash, secret, amounts, receiver invoice and channel. Stop
the foreground swap service first. The BTC gate must have the updated
`xbt-renew` RPC (restart the BTC launcher after applying the patch).

Renewal requires no controller state, no outgoing attempt, and a never-accepted
quote. It rechecks the predecessor cancellation, node identities, reserves,
channel liquidity and fees, and the original XBT invoice. The extension is at
most ten minutes and ends at least a minute before the XBT invoice expires.
The original terms and BTC invoice remain in a durable renewal journal.

An interrupted renewal can be retried with the same command during its window;
the gate recognizes the exact persisted request without extending it again.
The service refuses to run with an incomplete journal. A completed renewal can
only reprint its invoice, not extend it. If the original receiver invoice or
renewal window has expired, preserve the files for inspection. Do not remove
state or register a different hash to bypass these checks.
