# Receiving on a separate XBT node

The receiver creates an ordinary XBT BOLT11 invoice on its own node and gives
that invoice to the operator. The operator returns a priced BTC invoice for
the payer. The receiver keeps its wallet, RPC credentials and payment preimage.
The operator learns the preimage when its outgoing XBT payment completes.

This first workflow supports one explicitly selected direct XBT channel and
the existing pinned BTC payer channel. It adds no network-facing quote server,
remote wallet access, automatic funding or routing support. The existing
market caps, oracle freshness checks, quote gate and controller remain active.
An invoice handoff can be done over SSH/SCP without exposing either wallet RPC.

## VM preparation

Build the same XBT CLN fork in the VM and use a fresh dedicated Lightning data
directory. Do not copy a running node's wallet, hsm_secret or Lightning database.
The receiver can use the existing XBT Knots backend; no second blockchain copy
is needed. Knots must allow the VM's RPC connection. Keep backend credentials
private, as with the current operator launchers. Use a Python venv.

The current live_node.py launcher supports a localhost listener via
`--local-peer-port`. For a private initial connection, an SSH local forward
from the VM to the tower can reach the operator's localhost port 19836.
The VM then connects to that forwarded port using the operator's node ID.
Keep the tunnel running; it carries Lightning peer traffic, not wallet RPC.
No public listener is needed. A persistent VM service/tunnel is a separate
deployment step; the existing tower service manager does not manage this VM.

Open a new private channel from the XBT operator to the receiver and wait for
CHANNELD_NORMAL. Funding from the operator side supplies the receiver's
incoming liquidity. No on-chain deposit to the receiver is needed for that
channel. Check current market capacity before choosing a payment amount.

Export the receiver's node ID into a private text file on the VM and transfer
it to `$HOME/cln-live-pilot/vm-receiver-id.txt` on the tower. Transfer only the
ID and later the invoice, not wallet files or RPC credentials.

## Bind the operator to the new receiver

Run on the tower after the new channel is normal and connected:

```sh
.venv/bin/python tools/blake2b/remote_receiver.py \
  --source-config "$HOME/cln-live-pilot/operators-market.json" \
  --receiver-id-file "$HOME/cln-live-pilot/vm-receiver-id.txt" \
  --config "$HOME/cln-live-pilot/operators-vm.json"
```

This copies the existing caps, margin, operator CLI arrays and BTC channel
binding, selecting exactly one normal channel to the supplied XBT peer. It
uses only read RPCs to the operators, writes the new config with mode 0600,
and refuses to overwrite a different config. It creates no invoice, channel
or payment. Old configs and swaps remain unchanged. Binding alone does not
guarantee that a particular amount fits current market/fee limits; quote
creation still performs those checks.

## Invoice handoff and swap

On the receiver, create a fixed-amount XBT invoice with a unique label and
at least an hour of expiry. Save the BOLT11 string in a private text file.
Transfer it to `$HOME/cln-live-pilot/vm-xbt-invoice.txt` on the tower.

On the tower, with the payer ready (BTC quotes last at most 120 seconds):

```sh
umask 077
.venv/bin/python tools/blake2b/swap_service.py quote-market \
  --config "$HOME/cln-live-pilot/operators-vm.json" \
  --directory "$HOME/cln-live-pilot/vm-swap-1" \
  --xbt-invoice-file "$HOME/cln-live-pilot/vm-xbt-invoice.txt" \
  > "$HOME/cln-live-pilot/vm-quote-1.json"
```

Check the exit status before proceeding. On success, vm-quote-1.json contains
the BTC invoice and agreed amounts; keep it private. The invoice-file option
avoids entering the invoice in shell history, but the CLI RPC transport still
passes it as a subprocess argument to lightning-cli locally.

Start the existing service:

```sh
.venv/bin/python tools/blake2b/swap_service.py run \
  --directory "$HOME/cln-live-pilot/vm-swap-1"
```

Give the returned BTC invoice to the payer. The receiver checks its own
`listinvoices` result for `paid`; the payer checks completion. Controller
`btc_released` records release to the BTC hook, not independent confirmation
of the payer's final status. The operator cannot query the remote receiver's
wallet and does not need its RPC credentials.

Keep this swap directory under the existing cln-live-pilot recovery root.
The background recovery service scans these directories using operator RPCs
only. It does not start an unstarted swap; deliberately resume `run` if needed.
Its health fields about the local receiver still refer to the old local test
node, not the VM. A local receiver outage does not block operator recovery.

If interrupted, preserve the directory. Never recreate a quote or change its
invoice/config after publication. Use `invoice --directory ...` to retrieve
an unexpired saved quote, or `run --directory ...` to resume it. An expired
unused market quote needs a fresh receiver invoice and a new directory;
first confirm that no BTC HTLC was accepted and no XBT attempt was started.

The `receive` and `repay` convenience commands still assume local receiver
RPC access. Do not use them for this VM workflow. A remote receiver can later
pay an ordinary XBT invoice from the operator to return liquidity voluntarily.

## Web interface

CLN is the node daemon; a browser dashboard is an additional application.
Start9's CLN package bundles CLN Application as its Web UI. This fork currently
does not bundle or validate that frontend for XBT. Adapting an existing UI
requires checking network names, XBT invoice encoding, units, authentication
and RPC compatibility. A headless VM can serve a web UI accessed from another
computer; it does not need a desktop environment. The invoice-file workflow
does not depend on a GUI.

## Authenticated customer receiving workflow

Patch 0090 adds `/v1/receive` to the existing loopback API. It uses the same
customer-bound bearer credential and SSH tunnel. It remains disabled until
`receive_setup.py` explicitly stores a bounded `receive_config` in the operator
settings. The quote API's existing `--auto-process` option authorizes only new
receiving requests created after that enablement; old manual quotes and cached
requests are not upgraded. No public HTTP listener or customer RPC is added.

First run the unit and funded tests on the tower:

```sh
.venv/bin/python tools/blake2b/test_receive_api.py -v &&
.venv/bin/python tools/blake2b/test_customer_receive.py -v

.venv/bin/python tools/blake2b/receive_api_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli
.venv/bin/python tools/blake2b/receive_api_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fail-outgoing
```

The funded harness uses real regtest nodes, signed invoices, authenticated HTTP,
a committed incoming BTC HTLC and the existing outgoing controller. Its isolated
quote adapter supplies a fixed 1,500-sat price and regtest currencies; it does
not activate live policy or contact Neoxa. Separate unit tests exercise the real
live market quote preparation, caps and controller with mocked node RPCs.
Both funded cases check all four channel balances and exactly one outgoing
attempt across pending recovery. `--work-dir /tmp/new-short-path` retains logs.

After those pass, explicitly enable receiving with an existing, bounded market
configuration for this same customer and the two selected channels:

```sh
.venv/bin/python tools/blake2b/receive_setup.py \
  --config "$HOME/cln-live-pilot/operators-vm-receive-1.json" &&
systemctl --user restart cln-swap-recovery.service cln-swap-quotes.service
```

Setup checks identities, reserves and connected channels without pending HTLCs.
It preserves other settings and refuses a different already-enabled receive
configuration. Actual quote creation rechecks balances, oracle freshness,
amount caps, invoice recipient and exact channel bindings. The user service
must already run the quote API with `--auto-process` for automatic processing.

Install the same code on the customer VM. With the tunnel running, use a new
private directory for each new receiving intent:

```sh
.venv/bin/python tools/blake2b/customer_receive.py \
  --lightning-dir "$HOME/cln-xbt-customer" \
  --directory "$HOME/cln-customer-swaps/api-receive-1" \
  --xbt-sats 325000 --max-btc-sats 1480
```

This example is not a promise of liquidity or price; a previous forward swap
may have consumed the operator's XBT balance. The command creates an invoice
only on the customer's node, validates the returned BTC invoice's signature,
amount, currency, expiry and matching payment hash, then displays the BTC
invoice for the payer. Its output is private: share the invoice with the payer,
not public logs. A quote lasts up to 120 seconds. No customer payment RPC is
called. The customer is not required to keep the command running; the tower
worker starts the authorized swap once BTC is committed. Keep both nodes and
the peer connection available.

Rerun the exact same command to recover a lost reply or check receipt. It reuses
the original saved invoice label, request ID and offer. A paid invoice returns
only the receipt summary. Expired offers do not cause automatic requoting.
Known pre-creation refusals can be retried explicitly with `--retry-quote`;
unknown outcomes stay recorded for inspection. Preserve all attempt directories.
A new request ID cannot reuse an invoice already assigned to a request.

The worker checks a durable authorization digest under the same service lock
used by manual startup. No authorization means no new outgoing submission.
Started payments continue recovery even after their authorization expires.
The controller still saves submission intent before sending and never resends
an uncertain outgoing attempt. Manual forward swaps keep their previous
recovery-only behavior. Changing the bound customer requires a matching new
receiving configuration; the existing configuration is not silently retargeted.

## Unified customer command

`customer.py` wraps the existing send and receive workflows using only the
customer wallet. Defaults: wallet `~/cln-xbt-customer`, credential
`~/.config/cln-swaps/customer-api.json`, SSH-forwarded API
`http://127.0.0.1:19840`. New attempts are stored privately beneath
`~/cln-customer-swaps/managed`, separate from existing records.

Send BTC using XBT (still requires quote review and typing `PAY`):

```bash
.venv/bin/python tools/blake2b/customer.py send \
  --invoice-file "$HOME/cln-customer-swaps/fresh-btc-invoice.txt" \
  --max-xbt-sats 400000
```

The exact invoice selects a stable attempt ID. Repeating the command reuses
that attempt. Wallet, endpoint, invoice and limits are pinned before the
workflow begins; changes are refused. Submitted payments use the existing
reconciliation rules and are never automatically resubmitted. Preserve the
root and records, including after an unknown outcome.

Receive XBT from a BTC payer:

```bash
.venv/bin/python tools/blake2b/customer.py receive coffee \
  --xbt-sats 325000 --max-btc-sats 1480
```

This names the attempt `receive-coffee`. Repeat the same name and limits to
reuse the original invoice and offer. The output includes the BTC invoice for
the payer; keep it private. A different name deliberately creates a new
receipt. The example amounts remain subject to prices, liquidity and operator
caps. Expired offers are not automatically replaced.

List new attempts, or resume one using its saved arguments:

```bash
.venv/bin/python tools/blake2b/customer.py status
.venv/bin/python tools/blake2b/customer.py resume receive-coffee
```

Status reads files and queries the customer wallet only. It never creates an
invoice, requests a quote, prompts or pays; invoices, node IDs, hashes and
preimages are omitted. Resume may obtain a missing quote or show the original
review prompt for an unsubmitted send. After submission it reconciles through
the existing workflow. Use `--retry-quote` only after fixing a recorded
definite pre-creation refusal.

Inspect an older record without importing or changing it:

```bash
.venv/bin/python tools/blake2b/customer.py status \
  --directory "$HOME/cln-customer-swaps/api-receive-1"
```

Resume older attempts with their original scripts and arguments. No records
are migrated. Override `--lightning-dir`, `--token-file` or `--url` on send or
receive if needed; resume uses saved values. The global `--root` option must
precede the subcommand.

## Customer diagnostics

Customer commands report static public reason codes without returning raw RPC
errors, credentials, invoices, or node identities. Common quote refusals now
distinguish disconnected peers, unavailable channels, pending HTLCs, insufficient
inbound/outbound liquidity, and destination invoices too close to expiry.
A BTC no-route failure is identified as a disconnected-peer problem only when
all normal local BTC channels explicitly report disconnected. Otherwise the
existing bounded-route refusal remains applicable.

- `api_unreachable`: check the SSH tunnel and operator quote service.
- `api_credentials`: check the installed customer credential.
- `api_outcome_unknown`: preserve the original attempt and use resume to
  reconcile its original request. A timeout does not prove the request failed.
- `btc_peer_disconnected` / `xbt_peer_disconnected`: reconnect the relevant
  Lightning peer. In this pilot, opening Zeus restores its BTC connection.
- `*_channel_busy`: wait for pending HTLCs to resolve.
- `insufficient_*_receive_liquidity` / `insufficient_*_send_liquidity`: restore
  liquidity in the stated direction before explicitly retrying the quote.
- `invoice_expiring`: the destination invoice needs renewal; preserve the old
  attempt and establish its outcome before creating a replacement.

Only a definite refusal before quote-directory creation is recorded as
retryable with `--retry-quote`. Existing uncertain request records are not
reclassified. The API and recovery services need restarting after installing
these diagnostics; Lightning nodes do not.

## Invoice-selected receivers (opt-in development mode)

The `invoice-direct-v1` receiving policy chooses the XBT destination from the
signed invoice. It contains the operator CLI bindings, BTC incoming channel,
BTC/XBT caps and margin, but no receiver identity or XBT channel. The API
selects one normal, connected direct channel to the invoice payee with sufficient
outbound liquidity. Ambiguous channels, pending HTLCs, self-payment invoices,
wrong networks and invalid signatures are refused.

The selection is persisted before quote creation: original invoice digest,
payment hash, destination, short channel ID, channel ID and funding output.
Quote retries reuse that selection. A second request for the same payment hash
cannot create another selected quote, even if its invoice text differs. Before
a new outgoing attempt, the worker checks the original funding output and the
current receiving policy. Started attempts retain the existing recovery path;
they are never rerouted or repriced during recovery.

This mode uses `settings.receive_policy` instead of `settings.receive_config`.
The two cannot coexist. Its schema is:

```json
{
  "profile": "invoice-direct-v1",
  "btc_cli": ["<existing BTC operator CLI arguments>"],
  "xbt_cli": ["<existing XBT operator CLI arguments>"],
  "market": {
    "btc_channel": "<existing BTC channel>",
    "max_btc_sats": 1480,
    "max_xbt_sats": 327827,
    "margin_bps": 100
  }
}
```

A receive-only listener credential has `token` and `scope: "receive"`, with no
customer identity. It authorizes `/v1/receive` only; `/v1/quote` is rejected.
Customer receiving still records its own wallet identity for local recovery.
This removes receiver enrollment from this receiving path, not authentication
or the direct-channel requirement. It does not add routed XBT payments, public
network exposure, admission limits or a permissionless deployment. Legacy
credentials and the existing fixed-receiver live configuration keep their
original behavior. This patch supplies no live migration or activation command.

The funded development tests use two independently created XBT wallets and a
single unchanged policy and receive-only API credential. Each gets a distinct
invoice, selected channel and funding pin. They check original-offer replay,
pending recovery, exactly one outgoing attempt and all channel balances. One
variant rejects the second receiver's HTLC and verifies refunds. Market prices
and network labels use the existing regtest fixture adapter; actual Lightning
invoices, HTLCs, payments and channel balances come from the regtest nodes.

```bash
.venv/bin/python tools/blake2b/test_receive_selection.py -v
.venv/bin/python tools/blake2b/receive_multi_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli
.venv/bin/python tools/blake2b/receive_multi_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fail-second
```

### Unbound incoming BTC channel (experimental, opt-in)

`invoice-direct-v2` extends invoice-selected receiving by omitting
`btc_channel` from the receiving policy. Its market fields are only
`max_btc_sats`, `max_xbt_sats` and `margin_bps`. It creates a concrete
`live-market-v2` quote with `btc_channel_policy: any-normal-v1` and the
existing invoice-selected XBT destination/funding pin. The standalone BTC
quote plugin recognizes this profile only with an explicit matching live
opt-in. This patch does not change installed settings, credentials, launchers
or running services; the old receiving profiles remain supported.

Before publication, the operator checks that at least one connected, normal,
clear BTC channel has enough receivable liquidity and a sufficient HTLC
amount at its current fees. This check does not reserve a channel or guarantee
that a future incoming payment will use it. The first accepted HTLC binds the
quote durably to its actual incoming short channel ID and HTLC ID. Another
channel cannot replace that binding, including after plugin restart.

Before the worker spends XBT it verifies the exact incoming HTLC is committed,
normal, connected, untrimmed and matches the quote amount and expiry. It saves
`btc_incoming_pin` containing the original channel ID, funding transaction and
output, binding, payment hash, amount and expiry before invoking the controller.
The controller rechecks these fields immediately before submission. Pending
recovery does not select another incoming channel or resend XBT. The deadline
guard can close only the pinned funding channel. An ineligible incoming HTLC
never authorizes XBT spending; preserve its record for reconciliation.

The new profile is exercised through the receiving API/worker. It is not a
migration command or a public-service deployment. Authentication, one active
live quote, single-part payments and direct XBT delivery remain constraints.
No wallet RPC from the recipient is used by the operator.

Regtest exercises two BTC payer channels and two XBT recipient channels,
sequentially, with a BTC operator restart while each payment is pending. It
checks both successful settlement and rejection/refund of the second payment,
retained funding pins, unaffected other channels and one XBT attempt per quote.
Network labels and price acquisition use the existing fixed-price regtest
adapter; gate, wallet, worker and controller operations use real test nodes.

```sh
.venv/bin/python tools/blake2b/test_incoming_btc.py -v
.venv/bin/python tools/blake2b/receive_multi_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --any-btc
.venv/bin/python tools/blake2b/receive_multi_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --any-btc --fail-second
```

The regression catalog includes `unit-incoming-btc`, `receive-any-btc-success`
and `receive-any-btc-fail-second` (110 total cases).


### Customer XBT routing fee cap

For a new **send** attempt, `customer.py send`, `customer_swap.py`, and
`reverse_customer.py review` accept `--max-xbt-routing-fee-sats N` (0 through
1000). Supplying this option permits the customer's wallet to route its XBT
payment through intermediaries. Omitting it preserves the existing direct,
zero-fee policy; explicitly supplying zero permits only free routes.

`--max-xbt-sats` caps the total XBT amount, including the routing allowance.
For example, a 350000-sat quote with a 10-sat fee cap requires a total cap of
at least 350010 sats. The review shows the quoted XBT amount, routing-fee cap,
and maximum total before asking for `PAY`. This XBT fee allowance is separate
from the coordinator's BTC routing budget, which is priced into the quote.

The customer checks for a connected, normal first-hop channel with enough
spendable balance for the quote plus the full fee allowance. This does not
promise an end-to-end route. CLN `pay` chooses the route and enforces the saved
fee and delay caps. The service's existing single-HTLC admission rules remain
in effect; this change does not add multipath swap admission.

The routing allowance is saved with the wallet payment intent and, for the
unified command, with the managed attempt. Resume uses the original limits;
an existing reviewed or submitted attempt cannot be upgraded by passing a
larger cap. Submitted attempts only query payment status, even after a lost
reply, a failed payment, or a missing payment record.

Routed completion reports include exact `xbt_sent_msat` and
`xbt_routing_fee_msat`, and verify the preimage and both spending limits.
The legacy `xbt_sent_sats` field remains rounded down to whole sats; use the
millisatoshi fields for exact accounting of fractional-sat routing fees.

This option changes customer payment policy only. It does not migrate the
operator's live admission policy or change its authentication configuration.


### Public routed XBT delivery: controller regtest

`routed_receive_regtest.py` exercises BTC payer -> BTC coordinator, then XBT
coordinator -> XBT relay -> XBT receiver. There is no direct XBT channel between
the coordinator and receiver. The public route comes from `getroutes` with a
10-sat maximum fee, 80-block maximum route delay and one payment part. The relay
charges 5 XBT sats. This fixture exchanges 100000 BTC sats for 100000 XBT sats
at a fixed test price; it does not consult the oracle.

The controller's explicit `public-xbt-regtest-v1` mode validates both operator
networks and identities, the signed XBT invoice, route endpoint, fee and delay
limits, and the first-hop balance including fees. It requires the incoming BTC
HTLC to have at least the outgoing route delay plus 60 remaining blocks. That
is a controlled-regtest margin, not a live cross-chain timing policy. The route
and first-hop funding pin are saved before submission. Recovery checks the
original outgoing invoice, destination, amount and amount sent, including fees;
pending or unknown outcomes never trigger a resend or BTC refund.

Success and rejection cases stop and restart both coordinators while pending,
then reconcile through fresh controller processes. They verify all six channel
balances, the receiver invoice, relay fee and absence of pending HTLCs. The fee
refusal case lowers the saved fee cap below the planned fee: two controller
runs refuse without spending, and only the test harness returns the unspent BTC.

```sh
.venv/bin/python tools/blake2b/test_outgoing_xbt.py -v

for mode in success fail-outgoing fee-limit; do
  flags=()
  if [ "$mode" != success ]; then flags=("--$mode"); fi
  .venv/bin/python tools/blake2b/routed_receive_regtest.py \
    --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
    "${flags[@]}" || break
done
```

This is the controller foundation only. The receiving API's existing direct
selection remains unchanged. Private XBT route hints, routed market pricing,
and live activation require subsequent integration and tests. No live service
configuration or wallet is changed by these regtests.

### Routed receiving API fixture (regtest only)

The explicit `routed-receive-regtest-v1` receive policy connects the authenticated
`/v1/receive` API to public-route XBT delivery and the existing authorized
background worker. It accepts a 100,000-XBT-sat regtest invoice, selects one
public route of at most four hops, and pins its first funding output. There is
no fixed customer or incoming BTC channel. The actual committed BTC HTLC and
funding output are pinned before outgoing submission.

The saved Neoxa ask estimate prices the receiver amount **plus the full outgoing
XBT routing-fee allowance**, rounded up to whole XBT sats before pricing. The
XBT amount cap includes that allowance; the BTC quote must fit both operator and
request caps. The receiver still gets exactly its invoice amount. The quote
stores the audit, route, fee cap and funding pin under the worker authorization
digest. Worker steps never fetch another price or select another route. Unused
fee allowance is not retrospectively refunded. Exchange fees and the BTC payer's
own routing fees are outside this estimate.

This fixture policy is separate from live receiving profiles. It uses native
`regtest`/`xbt-regtest` identities and signed invoices, refuses mainnet operators,
and does not enable routed delivery for live services. Multipath, automatic route retries and live activation are not added here.
Private invoice hints are covered by the extension below.

Run from the repository root:

```sh
.venv/bin/python tools/blake2b/test_routed_receive_service.py -v

.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --api

.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --api --fail-outgoing
```

The funded tests replace only the exchange response with deterministic data:
100,000 receiver sats plus a 10-sat fee allowance costs 1,501 BTC sats at the
fixture price (1,500 without the allowance). The selected route actually costs
5 XBT sats. Real HTTP requests produce a stable offer; fresh background-worker
processes submit and recover the payment. Both operators restart while pending.
The tests check all six channel-side balances, the receiver invoice, original
payment attempt, incoming/outgoing funding pins and absence of pending HTLCs.
Success earns the relay 5 sats; rejection restores every channel balance.


### Private XBT final channels (regtest only)

Routed receiving also supports ordinary BOLT11 route hints from a signed XBT
invoice. A public route remains preferred. If the planner reports no route,
up to eight alternative private tails can be tried with a public prefix.
The prefix and tail share the same total 10,000-msat fee, 80-block delay and
four-hop limits. Tail fees compound with integer rounding. Invalid hints,
loops and over-budget tails cannot authorize spending; transport errors do
not trigger fallback. Invoice currencies are validated before the shared
route assembly logic runs.

The existing quote pins the complete selected route, allowance and first
funding output. Recovery uses that route and original attempt, never the
latest hint or a new route. The signed hint is routing information, not a
promise of remote liquidity or current forwarding policy. This remains
regtest-only and does not change live service configuration.

```sh
.venv/bin/python tools/blake2b/test_xbt_hints.py -v

.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --api --private-hint

.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --api --private-hint --fail-outgoing
```

The final XBT channel is unannounced and absent from the operator's gossip.
Removing the invoice hint must make planning fail. Both API cases preserve
fee-inclusive pricing, restart both operators while pending, and verify
all six channel-side balances, original payment attempt and matching invoice
outcomes. Only successful forwarding earns the relay its 5-sat fee.


### Routed API on-chain outcomes (regtest only)

Two additional fixtures keep BTC height fixed while resolving XBT on-chain.
Both use the authenticated API, a private final channel, saved quote/route,
operator restarts while pending, and fresh worker processes for recovery.

* `--onchain-preimage`: the relay closes its coordinator-facing channel with
  the incoming HTLC unresolved. The receiver then settles over the surviving
  final channel. The relay claims on-chain with that preimage, and the
  coordinator extracts it from the witness. The relay's delayed sweep is
  confirmed before the worker releases BTC.
* `--onchain-timeout`: the receiver stops. The relay closes the private final
  channel and recovers the outgoing HTLC after expiry and CSV delay. While the
  outcome remains pending, fresh workers preserve BTC. Only the original XBT
  attempt's definitive failure allows the worker to fail the bound BTC HTLC.

The relay uses a 30-block XBT forwarding delta in these fixtures (70 blocks at
route entry, 40 at the final hop). This stays within the existing 80-block
route cap and leaves room for final-hop timeout recovery without closing the
healthy upstream channel. It is not a live timing policy or a guarantee about
relative BTC/XBT block progress.

```sh
.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
  --api --private-hint --onchain-preimage

.venv/bin/python tools/blake2b/routed_receive_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
  --api --private-hint --onchain-timeout
```

Checks include confirmed HTLC spends and CSV sweeps, repeated pending and
terminal recovery without resending, original route/funding/quote bindings,
BTC balances and the surviving XBT channel balances. Closed-channel proceeds
incur XBT mining fees, so these tests do not claim that all XBT balances are
unchanged or that the relay's net profit is its nominal 5-sat routing fee.
The stopped receiver is not restarted in the timeout case. Reorgs, simultaneous
closure of both XBT channels, and live activation are outside these fixtures.


### Read-only live routed receiving inspection

`routed_receive_check.py` reads only the two operator RPC commands and their
pinned identities from private service settings. It does not contact a customer
wallet, create an invoice, publish a quote, authorize a worker, send a payment,
close a channel or change configuration. Existing live receiving remains direct.

Save a fresh XBT BOLT11 invoice in a user-owned file with mode `600`, then run
on the operator machine:

```sh
.venv/bin/python tools/blake2b/routed_receive_check.py \
  --invoice-file "$HOME/cln-live-pilot/routed-xbt-invoice-1.txt" \
  --max-btc-sats 1480 --max-xbt-sats 500000 \
  --max-xbt-routing-fee-sats 10 --margin-bps 100 --max-delay 288
```

The default settings path is `~/.config/cln-swaps/settings.json`. These command
line caps apply only to inspection; they never expand service authorization.
The XBT cap includes the full outgoing routing allowance. Neoxa asks price
that entire budget; the report separately shows receiver amount and actual
planned routing fee. Remote exchange execution and exchange fees are not
included. The BTC payer's own routing fees are outside this estimate.

Checks cover signed native XBT invoices, expiry/features, bounded public or
hinted routes, first-hop spendable liquidity, conservative untrimmed HTLC
minimum, eligible incoming BTC liquidity, advertised remote forwarding limits,
and 50,000 confirmed unreserved sats on each operator. Unknown private-hop
HTLC minima/maxima remain explicit blockers rather than assumed limits.
Metadata-bearing XBT invoices remain unsupported by this outgoing path.

The timing section is a candidate only: expected one BTC block per XBT block,
six XBT submission blocks, 144 BTC recovery blocks, and 24 BTC quote-drift
blocks, capped at 2016 incoming BTC blocks. Independent chain progress is not
guaranteed; no current incoming HTLC exists to validate. Thus
`live_timing_policy_checked` and `live_payment_enabled` remain false even when
all read-only checks pass. Remote liquidity is not knowable from gossip, and
`feasible` does not promise payment success. Use a fresh unpaid invoice: this
inspection does not establish whether the receiver has already settled it.

Output omits invoices, payment hashes, secrets, node IDs, routes and channel
identifiers. RPC errors expose only a stage, exception type and numeric error
code. Market validation errors that are not already whitelisted remain private.

```sh
.venv/bin/python tools/blake2b/test_routed_receive_check.py -v
```

### Candidate routed spending checks (regtest only)

`bounded-receive-regtest-v1` exercises the next receiving admission policy
through the authenticated API and background worker. It is not a live profile:
operator networks must still be `regtest` and `xbt-regtest`, invoices are still
fixed at 100,000 XBT test sats, and the fixture retains its 80-block outgoing
route cap. No installed configuration, activation record or service is updated.

The shared timing calculation assumes expected 1:1 block progress without
promising it. The quote pins a minimum incoming BTC margin of outgoing XBT
route delay + 6 submission blocks + 144 recovery blocks. Its signed BTC invoice
adds 24 drift blocks, with a maximum remaining margin of 2016. The controller
checks the held quote's exact policy and current BTC height again before send.
The read-only live checker now imports this same calculation.

Before registration and again before submission, the candidate checks 50,000
confirmed unreserved on-chain sats at each operator, the outgoing first hop's
conservative untrimmed threshold, and each advertised remote hop's current
fee, delay, active flag and HTLC limits. Unknown private-hop limits block this
candidate. There is no private-hint exception or live activation in this patch.
Existing private-route regtests remain on their original profile.

The selected route, funding output, timing and incoming BTC binding are stored
with the quote/controller. Recovery of an already submitted attempt does not
replan, reprice, or demand current admission reserves or gossip: it reconciles
the original payment. This patch does not add a new deadline-close policy.
A later live profile still needs explicit gate and deadline integration.

```bash
.venv/bin/python tools/blake2b/test_receive_bounds.py -v

for mode in success fail-outgoing stale-margin; do
  flags=()
  if [ "$mode" != success ]; then flags=("--$mode"); fi
  .venv/bin/python tools/blake2b/routed_receive_regtest.py \
    --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
    --api --bounded-policy "${flags[@]}" || break
done
```

Success and rejection restart both coordinators with pending payments, then
use fresh workers to settle or fail the original attempt and verify all six
channel-side balances. The stale-margin case advances only BTC until the
original held payment has one block less than required; two workers must
refuse before any XBT submission. The harness then fails the unspent BTC HTLC
and verifies restored balances. This cancellation is a fixture action, not an
automatic live refund policy.
