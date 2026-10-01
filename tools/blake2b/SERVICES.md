# User services for the existing live deployment

These helpers prepare four systemd user services and `cln-swaps.target` for the
existing BTC operator, XBT operator, XBT receiver and recovery monitor. They
never initialize new data directories, fund channels or create swap invoices.
The launchers still enforce their backend/network checks. The monitor pins the
three node identities observed during preparation.

The operator roots are the established `~/cln-btc-observe`,
`~/cln-xbt-observe` and `~/cln-xbt-peer`. The BTC listener remains at the saved
LAN address, port 19735. XBT listens only on localhost: operator 19836 and
receiver 19835. The monitor reconnects those XBT peers if needed. Zeus must
still reconnect to the BTC node from its own wallet.

## Prepare while the existing nodes are running

```
.venv/bin/python tools/blake2b/service_manager.py prepare \
  --config "$HOME/cln-live-pilot/operators-market.json" \
  --bitcoin-cli ../bitcoin-cli
```

This reads node identities, validates existing markers and CLI paths, writes
mode-0600 settings under `~/.config/cln-swaps` (directory mode 0700), and writes
units to `~/.config/systemd/user`. It starts nothing. Different existing unit
content or settings is refused rather than overwritten. Keep the repository,
venv and bitcoin-cli paths in place; the units use their absolute paths.

## Save credentials privately

Stop the foreground nodes using their RPCs, freeing the original shells:

```
./cli/lightning-cli --lightning-dir="$HOME/cln-btc-observe" --network=bitcoin stop
./cli/lightning-cli --lightning-dir="$HOME/cln-xbt-observe" --network=xbt stop
./cli/lightning-cli --lightning-dir="$HOME/cln-xbt-peer" --network=xbt stop
```

In the original BTC shell, which retains its exported variables:

```
.venv/bin/python tools/blake2b/service_manager.py credentials btc
```

It requires BTC_RPC_HOST, BTC_RPC_PORT, BTC_RPC_USER, BTC_RPC_PASSWORD,
BTC_RPC_CA and BTC_LN_HOST. The CA file must remain readable after reboot.
In an original XBT shell:

```
.venv/bin/python tools/blake2b/service_manager.py credentials xbt
```

It requires XBT_RPC_HOST, XBT_RPC_PORT, XBT_RPC_USER and XBT_RPC_PASSWORD.
The same settings serve both XBT nodes. Nothing is printed except the saved
kind. Do not paste credentials or these files into chat or Git. Missing values
cause refusal; re-export privately if a shell has lost them. No shell escaping
or systemd EnvironmentFile parsing is involved: private JSON preserves quotes,
spaces, dollar signs and backslashes. Runtime permission/ownership checks
reject group/world-readable files and symlinks. Secrets are local plaintext,
not encrypted; CLN inherits the required environment. User processes and root
with access to your account can still access them.

For credential rotation, update the exports and explicitly use `credentials
btc --replace` or `credentials xbt --replace`, then restart the corresponding
services. Capture uses no RPCs and performs no payments.

## Activate

Do not start a service while its foreground node is still running.

```
systemctl --user daemon-reload
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/cln-btc-operator.service" \
  "$HOME/.config/systemd/user/cln-xbt-operator.service" \
  "$HOME/.config/systemd/user/cln-xbt-receiver.service" \
  "$HOME/.config/systemd/user/cln-swap-recovery.service" \
  "$HOME/.config/systemd/user/cln-swaps.target"
sudo loginctl enable-linger "$USER"
systemctl --user enable --now cln-swaps.target
```

Lingering starts the user manager at boot and keeps it running after logout.
See https://www.freedesktop.org/software/systemd/man/252/loginctl.html .
The units run as the user, not root, with umask 0077. Nodes use their RPC stop
command for a graceful stop; systemd allows 120 seconds before final process
group cleanup. Failures restart after ten seconds; missing/invalid local
service configuration exits 78 and requires correction and manual restart.
Backend unavailability can cause launcher restart attempts until the backend
returns. The monitor independently polls readiness; target active alone does
not prove that CLN is ready.

After startup:

```
systemctl --user is-active cln-btc-operator cln-xbt-operator cln-xbt-receiver cln-swap-recovery
.venv/bin/python tools/blake2b/service_manager.py status
```

The health report updates about every ten seconds (RPC timeouts can delay it).
Expect `nodes_ready`, `operators_ready` and `xbt_connected` true, and no swaps
needing inspection or manual resume. The timestamp identifies stale reports.
Health output omits secrets, invoices, hashes and node IDs. Full CLN logs and
journal output may contain private identifiers; inspect those locally.

## Recovery boundary

The monitor scans `~/cln-live-pilot/*/quote.json` and
`~/cln-live-pilot/*/swap/quote.json`, covering both existing manual and wrapper
workflows. Paths outside this root and mismatched CLI/node bindings are refused.
For controller states with an outgoing attempt already recorded, it invokes
recovery under the same stable controller lock as the foreground workflow.
Unknown outcomes never authorize another XBT send. Recovery may settle or fail
the bound BTC HTLC after the corresponding definitive XBT result, or request
the existing BTC deadline close. It can therefore incur the same on-chain
close fees as the foreground watcher.

Prepared states are refused *inside* the controller lock by `recover_only`.
A held quote without controller state is reported as needing manual resume.
Neither case causes automatic payment submission, registration, signing or
repricing. Resume using the original foreground receive/service command and
canonical directory. Do not delete records. Terminal controller records are
retained and skipped. The monitor does not claim end-to-end receiver receipt
or final BTC peer settlement, and does not automatically process repayment
records; those remain reconciled by the original `repay` command.

A receiver outage does not block recovery when both operators are reachable.
An operator outage is reported and retried. A local malformed record produces
`needs_inspection`, not an unbounded retry of a spending operation. Polling
continues for other records.

New quotes still use the foreground receiving command. Closing that terminal
before XBT submission requires manual resume if a BTC HTLC arrives; once an
outgoing attempt is recorded, background recovery can reconcile it. This is
not an unattended invoice-issuing server.

To test a controlled restart when no new swap is being initiated:

```
systemctl --user restart cln-swaps.target
.venv/bin/python tools/blake2b/service_manager.py status
```

Wait for a new health timestamp and ready nodes. Stop the stack with
`systemctl --user stop cln-swaps.target`; disable future autostart with
`systemctl --user disable cln-swaps.target`. Do not run foreground launchers
alongside the services.
