# BTC backend over verified HTTPS

This observer launcher is separate from the XBT launcher and the regtest-only
swap service. It does not enable live swaps or request channel funding.
It starts CLN with `--network=bitcoin --offline` and an explicit data directory
by default. An optional LAN listener allows inbound peer and channel requests.

In the project's activated Python venv, export `BTC_RPC_HOST`, `BTC_RPC_PORT`,
`BTC_RPC_USER`, `BTC_RPC_PASSWORD`, and `BTC_RPC_CA`. The host must be the DNS
hostname (or IPv4 address) in the RPC certificate, without a URL scheme or path.
The CA is a local PEM certificate path. Credentials are not written to disk or
passed in process arguments. The generated adapter requires these variables
on every invocation, including after restarting the launcher.

```sh
python tools/blake2b/test_btc_https_cli.py -v
python tools/blake2b/live_btc_node.py --lightning-dir "$HOME/cln-btc-observe"
```

The launcher checks mainnet genesis, rejects the pinned XBT activation block,
and checks 80-byte SHA256d headers at activation height and the backend's tip.
These checks distinguish our XBT fork; they are not independent validation of
the backend's complete chain. A synced, unpruned BTC backend is required.
Only a new directory or one marked by this launcher is accepted.

From another terminal, no RPC environment variables are needed for:

```sh
./cli/lightning-cli --lightning-dir="$HOME/cln-btc-observe" --network=bitcoin getinfo
```

The adapter implements the subset of bitcoin-cli used by this revision of
`plugins/bcli.c`, not a general replacement for bitcoin-cli. It preserves raw
string results, empty null results and absolute RPC error exit codes. It
supports bcli's stdin arguments and startup wait flags. It uses verified TLS
with the supplied CA, disables environment proxies, and refuses redirects.
Connection and RPC error text is redacted. CLN's own local logs may still contain
transaction data, so keep the directory and logs private.

The adapter supports transaction submission for bcli, but neither this launcher
nor its startup checks request a transaction. Startup waiting only retries
`getnetworkinfo`; transaction submission is never automatically retried here.

## Optional LAN peer listener

Stop the existing BTC node cleanly before restarting the same directory with
`--listen-host "$BTC_LN_HOST" --listen-port 19735`. Set `BTC_LN_HOST` locally to
the tower's private LAN IPv4 address, not the StartOS RPC address. Never start
two lightningd processes against the same directory.

The listener binds only that address, disables automatic listeners, disables
discovered-address announcement and disables the peer seeker. It can still
reconnect to known peers; this is not an outbound firewall. Devices able to
reach that interface and port can connect. Do not add a WAN port forward.
Use a private/unannounced channel for the initial Zeus test. Connection URI:
`NODE_ID@TOWER_LAN_IP:19735`. The node ID and wallet are preserved on restart.
