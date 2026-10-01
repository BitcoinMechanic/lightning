# XBT Lightning receivers and optional swap operators

This repository bundles two related capabilities. The CLN fork provides XBT
chain support. Optional Python tools and a BTC-side plugin coordinate
BTC-to-XBT Lightning swaps. Bundling those tools does not require every XBT
node user to operate an exchange.

## Ordinary XBT receiver

A receiver runs one XBT Lightning node, with incoming channel liquidity, and
creates a normal XBT invoice. A BTC payer cannot directly pay that invoice.
A swap operator can offer a corresponding BTC invoice, accept BTC and pay
the receiver's XBT invoice using the same payment hash/preimage relationship.
The receiver does not need its own BTC node or a second XBT node.

The current live tools are a local, cooperative prototype: they explicitly
pin direct channels and the receiver. The local convenience workflow uses
receiver RPC access; the invoice-file workflow in REMOTE-RECEIVER.md does not.
A separately operated receiver can supply an invoice over a private file
handoff. This is not yet a public quote API or support for routed BTC payments
or arbitrary XBT destinations.

## Swap operator

An operator runs a BTC CLN instance, an XBT CLN instance, the BTC quote-gate
plugin and the swap controller. Each Lightning instance handles only its own
chain. The Python controller talks to them over RPC; there is no mixed-asset
channel or Lightning channel between the two operator instances.

The operator supplies outgoing XBT liquidity and incoming BTC capacity. Their
XBT balance falls and BTC balance rises when a BTC-to-XBT swap completes.
To receive additional XBT economically, the XBT must come from another party.
Operating all the wallets yourself exercises the protocol but moves your
existing XBT between your own wallets while adding the payer's BTC.

Atomic settlement is intended to remove custody trust in the exchange of
principal, not eliminate counterparties, liquidity, chain timing assumptions
or implementation risk. The oracle supplies a price reference, not coins or
settlement enforcement. This implementation remains experimental.

## Our four-node deployment

| Instance | Role | Currency |
| --- | --- | --- |
| Zeus | Payer | BTC |
| cln-btc-observe | Operator receives BTC | BTC |
| cln-xbt-observe | Operator supplies XBT | XBT |
| cln-xbt-peer | Separate receiver used in testing | XBT |

The extra XBT receiver is there to demonstrate delivery to a separate wallet.
A typical receiver using somebody else's operator runs only the last role.
A liquidity provider runs the two middle roles. Multiple instances on one
computer remain distinct Lightning nodes and wallets.

## Repository boundaries

- Native XBT chain parsing, hashing, network identity and wallet policy remain
  CLN functionality. Ordinary XBT Lightning operation does not require the swap
  gate, oracle, controller or service manager.
- `quote_plugin.py` is the optional BTC-side HTLC gate.
- `swap_service.py`, `swap_controller.py`, `swap_watch.py`, `deadline_guard.py`
  and policy modules implement the optional swap lifecycle.
- `swap_rpc.py` is the dedicated CLI RPC transport used by runtime tools. It
  does not start regtest backends, mine, fund channels, or import test harnesses.
  It preserves saved CLI argument arrays, JSON handling and the 20-second RPC
  timeout. It performs no automatic command retries.
- `smoke_regtest.py`, funded/swap scenarios and `regression.py` are test tools.
  They stay in this repository but runtime modules do not import them.
- Launchers, receiving commands and systemd helpers remain optional deployment
  conveniences. Existing services and saved state formats are unchanged by
  the RPC dependency cleanup.

A separate operator repository is not required. It could be extracted later
if independent packaging or release cadence becomes useful. BTC-side use of
unmodified upstream CLN with the plugin is an architectural goal, not a
verified compatibility claim: the live checkpoints so far used this fork's
binaries for both operator instances. Do not switch an existing wallet to
another binary just because the RPC interface looks compatible.

See MARKET-SWAPS.md for the bounded market profile, SERVICES.md for background
recovery, and ORACLE.md for pricing and capacity checks.
