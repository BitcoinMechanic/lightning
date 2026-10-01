# XBT to BTC: reverse swap development

Goal: an XBT Lightning payer supplies a BTC invoice from a receiver such as a
Phoenix user. An operator holds the payer's XBT HTLC, pays the BTC invoice,
and uses the BTC payment preimage to settle the XBT HTLC.

The two operator nodes still handle separate currencies. The operator needs
incoming XBT capacity and outgoing BTC liquidity. Receiving BTC in a previous
forward swap can build some outgoing BTC liquidity, but does not establish
that a route to a particular BTC receiver is available.

## Current checkpoint: disposable direct-channel fixture

`reverse_regtest.py` creates two isolated Knots regtest backends and four CLN
nodes. It funds a one-million-sat channel on each chain, then:

1. The BTC receiver creates an ordinary 100,000-sat BTC invoice.
2. The XBT operator signs a 200,000-sat XBT invoice with the same payment hash
   and an independent incoming payment secret.
3. The XBT payer calls ordinary `pay`; the test holding hook keeps that HTLC
   pending. Both commitments and the incoming test timelock margin are checked.
4. The BTC operator decodes the BTC invoice and submits one direct `sendpay`
   attempt using its payment secret. It does not obtain the receiver's preimage
   through receiver RPC.
5. On success, the preimage returned by the BTC payment resolves the incoming
   XBT HTLC. Both receipts, all four balance changes, one outgoing attempt, and
   the absence of pending HTLCs are verified.

The fixed exchange rate is solely a fixture, not a market quote. Test amounts
are regtest coins. The timelock check assumes both chains advance only under
the harness's control, not independent live mining.

With `--fail-outgoing`, a receiver-side fixture holds then explicitly rejects
the BTC HTLC. The harness waits for a terminal failed BTC attempt without a
preimage before failing the original XBT HTLC. It verifies all four original
balances and an unpaid BTC invoice. An RPC timeout alone is never treated as
proof that BTC failed.

```sh
.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli

.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fail-outgoing
```

Use `--work-dir /tmp/a-new-short-directory` to retain logs and node data for a
failure investigation. These are separate disposable wallets; current live
services, quotes and channels are untouched. The regression runner includes
both cases. The unsigned-invoice helper accepts XBT regtest (`xbtrt`) for this
fixture, but does not enable live XBT reverse invoices.

## Remaining before the Phoenix milestone

This is a protocol fixture, not a durable reverse swap service. Its holding
hook has no persisted quote admission, and the harness has no crash recovery.
Do not load it into a live node. The existing production-path development
remains BTC-to-XBT only; reverse live operation is not enabled by this patch.

Next work must cover an XBT-side quote gate and durable controller, recovery
of pending/complete/failed BTC attempts without accidental duplicate payment,
XBT deadline/on-chain protection, and routed BTC payments with a bounded fee
budget. The BTC invoice's final CLTV and route delay must constrain admission.
Forward ask-side oracle pricing cannot simply be reused as reverse pricing.
These behaviors must be tested before trying a small live Phoenix invoice.

The receiver can stay on the tower for development. StartOS packaging and a
web interface are independent later tasks.
