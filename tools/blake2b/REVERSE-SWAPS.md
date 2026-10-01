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
these cases and the recovery scenarios below. The unsigned-invoice helper accepts XBT regtest (`xbtrt`) for this
fixture, but does not enable live XBT reverse invoices.

## Controller crash recovery (CLN nodes stay running)

`reverse_controller.py` persists send intent before BTC `sendpay`, locks a
stable file beside the canonical state path, and never submits again after
that checkpoint. It accepts only the fixed-amount reverse regtest profile
and verifies both operator networks and identities before proceeding.

The controller checks the committed incoming XBT binding, fresh XBT-chain
margin, decoded BTC invoice and direct channel before initial spending.
Recovery requires exactly one BTC record with matching invoice, destination,
amount and amount sent. Pending preserves the checkpoint and held XBT;
complete validates and saves the preimage before releasing XBT; definite
failure is saved and rechecked before failing the exact original XBT HTLC.
Missing, multiple or inconsistent records do not authorize either a resend
or XBT resolution. Timeouts are not definitive failure evidence.

Run the three scenarios after the two basic cases:

```sh
for mode in --crash-after-btc --crash-while-pending --pending-failure; do
  .venv/bin/python tools/blake2b/reverse_regtest.py \
    --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli "$mode" || break
done
```

The first crashes after BTC completion but before saving its preimage. The
other two crash after submission with both payments committed, run two fresh
controllers while BTC is pending, then let the receiver settle or reject BTC.
Each verifies repeated terminal recovery, the original outgoing attempt,
end-to-end payer/receiver outcomes and all four channel balances.

These original modes are deliberately limited to controller restarts. Their holding plugin is
still nondurable. If a release/failure RPC response is lost after the hook
was answered, the controller preserves its btc_paid/btc_failed checkpoint
and refuses further resolution when the original HTLC is no longer present.
They cannot reconcile that terminal-intent window automatically; the durable
gate modes below cover it. There is still no reverse deadline protection.
Do not copy state files to another path or run them on multiple hosts: the
lock serializes one canonical state path only. No reverse live mode exists.

## Durable XBT quote gate and orderly operator restarts

`reverse_gate.py` is a separate XBT-regtest-only plugin. It imports the common
CLTV/replay validation and atomic persistence helpers from quote_plugin.py;
the forward gate and its live options are unchanged. The harness copies both
files into its disposable directory. Reverse admission maps the incoming XBT
amount to the shared validator internally; saved reverse terms retain explicit
XBT/BTC names. It admits only the fixed fixture amounts and direct channel.

Before BTC spending, the harness registers immutable terms including the BTC
invoice, hash, incoming payment secret, amounts, channel and expiry. The gate
validates the incoming HTLC/onion and persists its binding and immutable
snapshot before exposing it as held. On restart, an exact held replay restores
the hook even after quote admission expiry. Changed fields stay unresolved;
another binding cannot replace the original HTLC.

The controller compares its original quote, binding and absolute XBT expiry
with gate status before spending or resolving. Release/failure intent is
saved before the hook reply; replay returns that same terminal decision.
Lost controller replies can be reconciled from durable status without a
second BTC attempt or a second XBT resolution RPC. "Resolved" means durable
release intent, while the harness separately verifies the payer's final
settlement and channel balances.

```sh
.venv/bin/python tools/blake2b/test_reverse_gate.py -v

for mode in --pending-restart --pending-restart-failure \
            --release-recovery --failure-release-recovery; do
  .venv/bin/python tools/blake2b/reverse_regtest.py \
    --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli "$mode" || break
done
```

The restart cases stop and restart both operator CLN nodes while both HTLCs
are committed, check unchanged quote/binding snapshots and recover the same
BTC attempt to completion or rejection. Payer, receiver and backends stay up.
The resolution cases crash the controller after the XBT gate reply but before
the final controller checkpoint. Offline tests separately inject an RPC
timeout after persisted gate intent. These are orderly node-restart tests,
not SIGKILL/power-loss or chain-stall tests.

## Remaining before the Phoenix milestone

This remains a regtest protocol fixture, not a live reverse swap service.
Do not load it into a live node. The existing live-path development
remains BTC-to-XBT only; reverse live operation is not enabled by this patch.

Next work must cover XBT deadline/on-chain protection and live BTC invoice
routing needs (including private route hints), beyond the bounded public-route
fixture below. The BTC invoice's final CLTV and route delay must constrain admission.
Forward ask-side oracle pricing cannot simply be reused as reverse pricing.
These behaviors must be tested before trying a small live Phoenix invoice.

The receiver can stay on the tower for development. StartOS packaging and a
web interface are independent later tasks.

## Routed BTC fixture and fee budget

`routed_reverse_regtest.py` creates five CLN nodes: XBT payer/operator and BTC
operator/relay/receiver. There is no BTC operator-to-receiver channel. The
relay charges 5 sats to forward the fixture's 100,000-sat BTC payment. The XBT
leg remains 200,000 sats and uses the durable reverse gate.

`reverse_route.py` calls the pinned CLN `getroutes` RPC with `auto.localchans`,
`auto.sourcefree`, `maxparts=1`, a 10-sat maximum fee and 80-block maximum
outgoing delay. It converts the v26.06 path format to sendpay hops using each
channel's far-end amount/CLTV. It independently checks path continuity,
direction, absence of loops, fee, amount and delay bounds and the exact final
destination/amount. At most four hops and a 40-block final CLTV are supported
by this regtest profile. The current fixture has two BTC hops.

The chosen route and limits are persisted with controller state before
submission. The controller independently enforces those limits and checks
that first-hop liquidity covers the BTC amount plus fees. Its fresh XBT
margin must be at least max(100, outgoing route delay + 60) blocks. This is
still a controlled-regtest check, not a guarantee about independent live
chain timing. Recovery validates the recorded BTC amount sent including fees,
and never calls the route planner or resubmits the payment.

```sh
.venv/bin/python tools/blake2b/test_reverse_route.py -v

.venv/bin/python tools/blake2b/routed_reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli

.venv/bin/python tools/blake2b/routed_reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fail-outgoing

.venv/bin/python tools/blake2b/routed_reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fee-limit
```

The success/rejection cases restart both operator nodes while the routed BTC
payment is held at the receiver. Recovery must settle or fail the original
attempt; the test checks all six channel-side balances across the five nodes.
On success the relay earns exactly 5 sats; on failure balances are restored.

The fee-limit case deliberately hands the controller the known 5-sat route
with a 4.999-sat budget. Two fresh controllers must refuse without a BTC
attempt or state change. The harness then explicitly fails the unspent XBT
HTLC and verifies restored balances. This exercises the controller's check
even if a route candidate was obtained with a more permissive planner budget.

There are no automatic retries, multipart payments or blinded paths. Live
reverse swaps remain disabled.

## Private BOLT11 route hints (regtest)

If the public route query returns CLN error 205 (no route), the planner can
use a signed invoice's `routes` hints. It calculates each private tail's
fees and CLTV backwards using integer arithmetic, then queries a public
prefix to the hint entry with the remaining fee budget. At most eight
alternative hints are considered, and the first fitting route is selected.
Other RPC failures and timeouts propagate without fallback. Planning calls
only `getroutes`; it never changes shared gossip or askrene layers.

The complete public/private path must still fit the 10-sat total fee,
80-block delay and four-hop caps. Loops, malformed hints and mismatched
prefix responses are refused. The recorded route uses the hint's channel
identifier, including a private channel alias. Recovery uses that same
recorded route/attempt without replanning or sending another payment.

The private fixture opens the relay-to-receiver channel with
`announce=false`, waits for its fee update before creating the invoice,
and explicitly includes its private hint. It proves the operator has no
public gossip for that channel and cannot plan to the receiver without
the hint. Success and rejection both exercise operator restarts and
controller recovery, and check all six balances and pending HTLCs.

```sh
.venv/bin/python tools/blake2b/test_reverse_hints.py -v
.venv/bin/python tools/blake2b/test_reverse_route.py -v

.venv/bin/python tools/blake2b/routed_reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --private-hint

.venv/bin/python tools/blake2b/routed_reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
  --private-hint --fail-outgoing
```

This adds ordinary BOLT11 hint support, not a claim of complete Phoenix
compatibility. Live reverse quoting, independent-chain deadline handling
and a bounded live workflow remain separate work.


## Incoming XBT on-chain claim (regtest)

`--onchain-claim` holds both payments, crashes the controller after BTC
submission, then has the harness close the XBT operator's channel. The BTC
receiver resumes settlement only after the unresolved XBT commitment is
confirmed. The controller recovers the BTC preimage and releases the original
XBT hook to CLN's onchaind. The test crashes it again after that durable release,
then reconciles twice without another BTC attempt or another hook release.

This uses an explicit `xbt_onchain_claim` opt-in. Before BTC submission, the
controller records the verified incoming channel ID, funding outpoint, peer
and short channel ID. On-chain release requires that same channel, the original
HTLC and hook, the durable quote binding, and a verified BTC completion preimage.
It does not require the peer to remain connected once the channel is on-chain.
New BTC spending and XBT failure resolution still require a normal channel.

The test verifies the confirmed HTLC-success witness, the payer's extraction
of the preimage, CSV maturity and the operator's confirmed unspent sweep output.
The BTC channel balances must match the agreed payment and there must be exactly
one BTC attempt. The BTC chain stays fixed during XBT recovery. XBT on-chain
fees apply; equal Lightning balances are not expected for the closed channel.

```sh
.venv/bin/python tools/blake2b/test_reverse_onchain.py -v
.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --onchain-claim
```

`xbt_released` means the durable hook resolution was reconciled; it does not
assert that the on-chain claim or wallet sweep is confirmed. The harness checks
those confirmations independently. The close in this test is harness-triggered,
not an automatic deadline guard. Independent-chain deadline policy, BTC outgoing
on-chain outcomes, reorgs and live reverse use remain outside this checkpoint.


## Automatic incoming XBT deadline close (regtest)

`--xbt-deadline` extends the on-chain claim fixture with an opt-in
`xbt_deadline_guard`. Each controller reconciliation of the verified pending
BTC attempt checks remaining XBT blocks. Above 30 it leaves the channel open;
at 30 or below it requests a unilateral close of the original pinned XBT
channel. No BTC resend or XBT HTLC resolution is authorized by the deadline.
The controller must be invoked for this check to run; this patch adds no
background scheduler or live service integration.

The guard requires the durable quote binding, regtest operator identity,
original funding outpoint/channel/peer and exact incoming HTLC. It saves the
close intent before calling `close` with the channel ID. After an interrupted
RPC, reconciliation recognizes CLN's unilateral-close states without issuing
another close. If the RPC never ran, it retries only the recorded channel
after rechecking the pin and HTLC. An unknown BTC outcome remains an error.

The fixture advances only XBT: it verifies no close at 31 blocks remaining,
a controller-triggered close at 30, and unchanged pending payments on a fresh
controller invocation. Then it reuses the confirmed-commitment, BTC settlement,
release-crash, on-chain preimage and confirmed CSV sweep checks.

```sh
.venv/bin/python tools/blake2b/test_reverse_deadline.py -v
.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --xbt-deadline
```

Thirty blocks is a controlled-test threshold, not a live cross-chain safety
guarantee. A close alone does not prevent loss if BTC reveals a preimage only
after the XBT refund wins. Live reverse timing policy and outgoing BTC on-chain
outcome tests remain separate work. Live reverse swaps remain disabled.


## Outgoing BTC on-chain outcomes (regtest)

`--btc-onchain-preimage` and `--btc-onchain-timeout` exercise the other
on-chain leg. The XBT channel stays open and the XBT chain stays fixed.
The controller is stopped after its original BTC submission checkpoint;
fresh controllers must preserve the pending XBT payment while BTC is unresolved.

In the preimage case, the BTC receiver publishes its commitment and claims the
held HTLC on-chain with a harness-only secret. The pending controller state has
no secret. The fixture verifies the confirmed witness, BTC operator onchaind
extraction, and receiver CSV sweep before recovering the controller. Only the
BTC operator's matching completed payment record supplies the preimage used to
resolve the bound XBT HTLC. XBT Lightning balances must match the agreed amounts.
The harness directly resolves the receiver's holding hook, so receiver invoice
status is not used as evidence of this on-chain payment.

In the timeout case, the receiver goes offline. The BTC operator closes and
recovers the HTLC through a confirmed timeout and CSV sweep. Before expiry,
fresh controllers must leave XBT held without changing either durable record.
After the refund, the original BTC attempt must be definitively failed with no
preimage before the controller may fail the bound XBT HTLC. XBT balances must
return to their starting amounts.

Both cases crash the controller again just after durable XBT resolution and
reconcile twice. They check exactly one original BTC attempt, unchanged quote
terms/binding, empty XBT HTLCs, and the expected XBT payer outcome. BTC on-chain
fees apply. This patch adds fixtures and leaves controller behavior unchanged.

```sh
.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --btc-onchain-preimage

.venv/bin/python tools/blake2b/reverse_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --btc-onchain-timeout
```

These are controlled, direct-channel regtests. They do not cover both chains
closing simultaneously, reorgs, or arbitrary live chain stalls. Live reverse
swaps remain disabled.
