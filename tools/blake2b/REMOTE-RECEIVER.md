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
