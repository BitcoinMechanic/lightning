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
