# Experimental BTC → XBT regtest service

This milestone provides an interactive workflow with real CLN regtest nodes:
create an XBT invoice, quote a BTC price, pay the BTC invoice, and receive XBT.
The operator supplies liquidity on both chains. Prices are explicit, not an
exchange-rate feed. Only a direct XBT channel and single-part BOLT11 are supported.

## Start the funded lab

From the repository root, in terminal 1:

```bash
source .venv/bin/activate
python tools/blake2b/swap_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
  --service-lab --work-dir /tmp/xbt-service-lab
```

Choose a **new, short directory** if that path already exists. Wait for
`Interactive regtest lab ready`. Leave this terminal running. It starts two
isolated Knots backends and four CLN nodes, funds the channels, and installs
the BTC quote plugin. It never connects to your VM's Knots node.

The BTC payer has a channel to the BTC operator; the XBT operator has a channel
to the XBT receiver. Each starts with a 1,000,000-sat funded channel. The CLI
wrappers and `operators.json` are written into the lab directory.

## Create and price a swap

In terminal 2, from the same repository root:

```bash
source .venv/bin/activate
/tmp/xbt-service-lab/receiver-cli invoice \
  200000000msat receive-1 'Receive XBT through BTC swap' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["bolt11"])' \
  > /tmp/xbt-service-lab/xbt-invoice.txt

python tools/blake2b/swap_service.py quote \
  --config /tmp/xbt-service-lab/operators.json \
  --directory /tmp/xbt-service-lab/swap-1 \
  --xbt-invoice "$(cat /tmp/xbt-service-lab/xbt-invoice.txt)" \
  --btc-sats 100000 \
  > /tmp/xbt-service-lab/quote-1.json

cat /tmp/xbt-service-lab/quote-1.json
```

This quotes **100,000 BTC sats for 200,000 XBT sats**. That ratio is your chosen
test price. The signed BTC invoice shares the XBT invoice's payment hash.
Quote validity is at most ten minutes and leaves sixty seconds before the XBT
invoice expires. Capacity is checked but not reserved across separate swaps.

## Run the service and pay

Still in terminal 2:

```bash
python tools/blake2b/swap_service.py run \
  --directory /tmp/xbt-service-lab/swap-1
```

It waits for the BTC HTLC to be fully committed, submits the original XBT
payment, and continues reconciliation automatically. Leave it running.

In terminal 3, from the repository root:

```bash
source .venv/bin/activate
btc_invoice=$(python -c 'import json; print(json.load(open("/tmp/xbt-service-lab/quote-1.json"))["btc_invoice"])')
/tmp/xbt-service-lab/payer-cli pay "$btc_invoice"

python tools/blake2b/swap_service.py status \
  --directory /tmp/xbt-service-lab/swap-1

/tmp/xbt-service-lab/receiver-cli listinvoices receive-1
```

The payer should report `complete`, the receiver invoice `paid`, and service
status `btc_released`. Service status reports a durable release checkpoint;
the payer and receiver RPCs establish actual settlement. Terminal 2 exits
after recording release or failure intent. It does not stop the nodes.

Use a fresh receiver invoice label and a fresh swap directory for each swap.
Do not reuse or copy a swap's state directory to launch another payment.
Repeated swaps consume channel liquidity; this lab does not rebalance it.

## Recovery and shutdown

Restart the exact same `run --directory ...` command after a service crash.
It resumes the recorded attempt, not a new XBT payment. Concurrent service
processes using the same directory report `busy`. Do not delete `quote.json`,
`state.json`, or lock files, and do not edit them while a service is running.
These files and the quote plugin's adjacent `.quotes.json` file must be retained.

If quote setup was interrupted before printing its invoice, finish publication:

```bash
python tools/blake2b/swap_service.py invoice \
  --directory /tmp/xbt-service-lab/swap-1
```

Identical registration retries preserve the quote phase and original binding.
Changed terms for the same hash are rejected. Expired quotes require a fresh
receiver invoice and swap directory. `status` is a local checkpoint view and
does not prove the service is currently running.

RPC errors are retried during pending recovery. Invariant errors stop the
service for inspection. A pre-spend refusal leaves the BTC HTLC held and sends
no XBT; inspect the reported reason before intervening. Do not manually fail
a BTC HTLC if an XBT outcome is unknown.

Ctrl-C in the service terminal stops polling; it does not cancel a submitted
payment. Restart `run` to resume. Stop terminal 1 only after swaps settle:
Ctrl-C there stops all six nodes. `--work-dir` retains their files, but this
launcher only creates fresh labs; it is not a node restart manager.

The deadline guard is enabled for service-created swaps. At thirty BTC blocks
remaining, pending reconciliation requests a unilateral close. Lab chains do
not mine automatically. If experimenting with closures, fund the BTC operator
wallet for fees and mine the relevant chain using the `btc-chain-cli` or
`xbt-chain-cli` wrapper. A close cannot guarantee recovery if XBT remains
unresolved beyond the BTC claim deadline. This is a regtest service, not a
real-funds deployment or a chain-stall/reorg safety guarantee.

## Existing regtest operators

Instead of the lab's generated config, supply JSON with exactly two argument
arrays. Use absolute executable and data-directory paths, for example:

```json
{
  "btc_cli": ["/absolute/lightning/cli/lightning-cli", "--lightning-dir=/absolute/btc-operator", "--network=regtest", "--json", "--notifications=none"],
  "xbt_cli": ["/absolute/lightning/cli/lightning-cli", "--lightning-dir=/absolute/xbt-operator", "--network=xbt-regtest", "--json", "--notifications=none"]
}
```

The BTC operator needs a persistent copy of `quote_plugin.py` loaded, with an
executable shebang pointing at this project's venv Python. Use a separate copy
per BTC operator; the plugin stores quotes beside itself. The lab handles this
automatically. The XBT operator must have exactly one usable direct channel to
the invoice recipient. The BTC payer needs a route to the BTC operator; the
service does not add private-channel route hints or support MPP.

Both node networks and their saved identities are checked. BTC and XBT mainnet
nodes are refused. Run this foreground service under your own supervision if
you want automatic process restarts; no system service is installed here.

## One-command verification

```bash
python tools/blake2b/swap_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --service-demo
```

This exercises publication, payment waiting, automatic settlement, status,
and terminal restart with one original XBT attempt. It uses a 123,000-sat BTC
price to exercise variable quote amounts. The regression catalog includes
`swap-service-demo` and `unit-swap-service`.

## Held quote recovery after expiry

Quote expiry and relative CLTV admission limits apply to new HTLCs only. An
already-held HTLC is restored after restart only when its immutable fields
match the snapshot persisted at acceptance. Recovery does not rewrite that
snapshot or cancel BTC merely because the quote expired or blocks advanced.

Legacy held records without an acceptance snapshot, or inconsistent replays,
remain unresolved and are not exposed as releasable hooks. They require local
inspection; never delete state or fail BTC while XBT may still settle. Start
fresh regtest cases after applying this update. This does not enable live swaps.
