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


## Read-only reverse market reference

`reverse_oracle.py` prices a specified whole-sat BTC amount plus an explicit
maximum routing-fee allowance from Neoxa's ordinary bid depth, with an operator
markup and upward whole-sat XBT rounding. See ORACLE.md for the command and
accounting. It calls only the public exchange API, not any Lightning node.
This is a separate price inspection tool: it does not change the regtest
controller's fixed amounts, create a reverse quote, or enable live payments.


## Read-only live invoice and channel inspection

`reverse_check.py` inspects a local BTC BOLT11 invoice without sending payments,
creating quotes, connecting peers, reserving routes, changing gossip layers or
writing swap state. It uses only getinfo, decode, listpeerchannels, listfunds and
getroutes, plus the public Neoxa API. It creates no service and restarts no node.

Default roles on this tower are:

| Role | Lightning directory | Network |
| --- | --- | --- |
| BTC operator | ~/cln-btc-observe | bitcoin |
| XBT operator | ~/cln-xbt-observe | xbt |
| XBT payer (former receiver) | ~/cln-xbt-peer | xbt |

Overrides are `--btc-dir`, `--xbt-operator-dir`, and `--xbt-payer-dir`. These are
read-only local RPC targets, not a saved or trusted live payment configuration.
A future live workflow must persist explicit identities and channel bindings.

Use a fresh fixed-amount BTC BOLT11 invoice, initially 1,500 sats. Store it in a
local file owned by your user with no group/other permissions (0600). Symlinks,
multiple invoices and files over 32 KiB are refused. The invoice is not sent to
Neoxa, printed in the result, or placed in a swap record. It is passed to the
local lightning-cli decode command; privileged local process inspection and
CLN logs are outside the output-redaction boundary.

```sh
.venv/bin/python tools/blake2b/reverse_check.py \
  --invoice-file "$HOME/cln-live-pilot/btc-invoice-1.txt" \
  --max-routing-fee-sats 10 --margin-bps 100
```

The inspection accepts whole-sat BTC amounts up to 10,000 sats, an explicit fee
allowance up to 100 sats, and an estimated XBT cap of 500,000 sats by default.
It checks the signature, currency, required invoice features, payment secret,
expiry (at least two minutes left), and final CLTV (at most 144 blocks). BOLT12 and unsupported required feature bits are refused. Well-formed BOLT11
payment metadata up to 512 bytes is accepted for inspection, including required
feature bit 48; that bit without decoded metadata is refused. The report exposes
only whether metadata is present, never its contents. Optional
MPP does not require splitting: this check selects only a single-part route.

The route is limited to eight hops and defaults to 288 outgoing blocks, including
ordinary private BOLT11 hints. Read-only inspection accepts `--max-delay` from
1 to 2016 blocks; for the Phoenix pilot use an explicit `--max-delay 576`.
Inspection tries private hints after bounded public-route failures (205 or 206),
without increasing the chosen fee or delay caps. These are inspection bounds, not an approved live swap
timelock policy. The regtest controller and its default route validation retain
the original fixed amounts and narrower limits; live payment remains disabled.

The report compares the market XBT charge against the payer's spendable and
operator's receivable balance on the same funding channel, checks conservative
untrimmed minimums at both XBT endpoints and the BTC first hop, includes fees in
BTC spendable capacity, and reports the existing 50,000-sat confirmed unreserved
operator reserve check. Historical closed channels are ignored, but multiple
normal channels between the XBT nodes are treated as ambiguous.

The inspector queries `listchannels` separately for each remote hop's short
channel ID, and matches its exact source and destination direction. It checks
advertised HTLC minimum/maximum, enabled status, fee and CLTV delta against the
amount and delay on that hop. Known violations make `feasible` false. Missing
policies also make it false, with a separate unknown-hop count: BOLT11 private
hints contain fees and delays but no HTLC minimum or maximum. A missing policy
is not evidence that a payment would fail, nor permission to mark it verified.
Transport errors and malformed or ambiguous policies abort inspection.

`remote_btc_htlc_minima_checked` means all remote hop policies were available;
`remote_btc_htlc_limits_passed` additionally requires no policy violations.
Neither field establishes actual remote liquidity or freshness of gossip.

`feasible` means only these read-only checks passed. No remote BTC channel's
dust/untrimmed minimum is known, no liquidity is reserved, and no live independent-
chain timing policy is validated. The selected route can still fail. The result
explicitly reports these limits and omits payment hashes, secrets, invoices,
node IDs, channel IDs and funding outpoints. A route/amount infeasibility is a
normal result; RPC, malformed data and invoice errors exit nonzero with redacted
messages. A stale price or invoice during inspection requires a fresh check.


Payment metadata is opaque receiver data, not something to discard. The pinned
CLN `sendpay` schema accepts `payment_metadata` as hex and the implementation
places it in the final onion hop. Passing `bolt11` alone does not supply those
bytes. The read-only inspection does not construct an onion, verify full onion
payload size, persist metadata or call sendpay. A future live reverse workflow
must bind it to the signed invoice and forward it unchanged. The reverse regtest
controller now validates and checkpoints metadata before submission, and passes
it explicitly to sendpay. Recovery continues to reconcile the original attempt
without resending. Live reverse execution remains disabled.


## Candidate reverse timing policy (read-only)

`reverse_timing.py` contains a proposed risk budget, not approved live execution.
The matching Knots tag `v29.4.2.knots20260508` retains a 600-second mainnet target
spacing (`src/kernel/chainparams.cpp`). That target does not bound actual block
arrival times. No finite expiry gap guarantees safety if BTC stalls while XBT
keeps progressing, or XBT accelerates sufficiently. Closing the XBT channel
cannot create a missing BTC preimage or extend the incoming HTLC expiry.

The v2 candidate uses a 1:1 expected block pace because both chains target
ten-minute blocks. This is not a maximum relative rate or a guarantee. Recovery
headroom is an explicit separate 144-XBT-block margin; the earlier arbitrary
four-to-one stress multiplier has been removed. The model version changes, so
v1 proposals are rejected rather than silently reinterpreted by pre-spend checks.

- Minimum incoming XBT blocks remaining: `BTC route delay + 6 + 144`.
- The six BTC blocks provide submission-height slack at the expected 1:1 pace.
- Proposed invoice delta: that minimum plus 24 XBT blocks of quote drift.
- Maximum incoming delta: 2016, matching the pinned CLN default HTLC CLTV cap.
- Maximum supported BTC route delay under those constants: 1842 blocks.

For the observed 448-block route this gives a 598-block minimum and a 622-block
invoice request. The 144-block reserve remains a proposed operational margin,
not a measured statistical bound. A different route must be evaluated anew.
The report never truncates the computed requirement to fit the maximum.

While pending, the model requires `BTC blocks remaining + 144` XBT blocks
remaining. Equal advancement preserves the margin; XBT-only advancement erodes
it. Monitoring detects divergence but cannot guarantee recovery after arbitrary
BTC stalls or XBT acceleration. Pending reports describe this condition only:
automated live monitoring and response have not yet been integrated.

`reverse_check.py` exposes the candidate under `timing_proposal`. It leaves
`live_timing_policy_checked` and `live_payment_enabled` false. Passing arithmetic
alone does not verify current chain progress, local payer maxdelay configuration,
fee reserves over that lifetime, authenticated held HTLCs, or service monitoring.

`pre_spend_report` consumes actual incoming expiry and fresh per-chain heights,
without comparing absolute heights between chains. Its BTC planning upper expiry
is not an observed outgoing HTLC expiry. Future submission integration must bind
and persist the actual attempt and expiries. `pending_report` uses actual expiries
to flag margin erosion and the recovery reserve. Neither a breach nor an elapsed
BTC expiry proves payment failure; BTC outcome must still be reconciled before
XBT can be failed. These helpers perform no RPC or state mutations and do not
change the existing regtest deadline guard.

## Dormant reverse service integration

Patch 0074 adds `reverse_live.py` and `reverse_service.py`, plus explicit
live-profile paths through the existing gate, controller and recovery scanner.
`LIVE_EXECUTION_ENABLED` remains false. Do not change it manually: the node-backed
integration and persistent gate launcher setup must be verified before activation.
Applying this patch does not register a live quote, publish an invoice, restart a
node or send funds. Existing regtest profiles retain their amounts and limits.

The staged profile is restricted to a 1500-sat BTC invoice, at most 500000 XBT
sats, a 30-sat BTC routing allowance, a 1% market margin, a 576-block route search
cap and one immutable route per quote. Quotes last at most five minutes. The
market snapshot is checked before quoting; an accepted price is not silently
recalculated after receiving the incoming payment.

Private-final policy is explicit in the quote: exactly one unknown remote policy
may be accepted only if it is the last hop and belongs to an exact tail from the
signed BTC invoice. All available policies still have to pass. This exception
does not assert knowledge of that channel's HTLC limits or liquidity; a receiver
rejection is handled as an outgoing payment failure. Unknown intermediate hops,
wrong directions/channels and known policy violations remain disallowed.

`quote` uses private service-manager settings, creates an exclusive directory
immediately under the monitored swap root, saves terms before registration,
registers the durable XBT gate, and verifies the signed XBT invoice before
publishing it. A lost registration/signing reply leaves a private draft for
inspection; it is not automatically deleted or re-quoted. No payer payment is
originated by this service.

`run` waits for the original held XBT HTLC, persists controller state and performs
one explicitly started BTC attempt. It rechecks current timing, pinned identities,
quote binding, metadata, reserves, first-hop capacity and remote advertised
policies before spending. Submission is checkpointed before the RPC, including
both original channel funding identities. Ambiguous outcomes never resend BTC.

While pending, the controller records the original BTC HTLC ID and actual expiry
when observable. Before that observation it uses the explicitly labelled planning
upper expiry, including the six-block submission slack. Both chain heights feed
the 1:1 candidate model; margin erosion or the 144-XBT-block reserve can trigger a
close of the pinned incoming XBT channel. Closing cannot extend its HTLC expiry
or eliminate cross-chain stall risk. BTC expiry alone never permits XBT failure.
The existing verified-preimage path supports incoming on-chain claims.

The existing recovery service recognizes `reverse-quote.json` and
`reverse-state.json` separately from forward swaps. It validates service identities,
serializes with manual service commands, and invokes controller recovery-only mode.
It never registers/signs a quote or starts a prepared BTC payment. A held quote
without controller state is reported as requiring manual start. Missing or
inconsistent records require inspection.

`abort-unspent` is restricted to an existing prepared state under both service and
controller locks, with no recorded BTC attempt. It checkpoints cancellation before
resolving the exact original XBT gate binding. Lost failure replies reconcile
through durable gate status. Submitted or ambiguous BTC attempts cannot use this
command. Unrelated XBT invoices continue through the live gate to normal CLN
invoice handling.

The integration tests use fake RPCs and temporary private state. They explicitly
opt into the dormant profile only inside the test process; real-node activation
is still blocked. No live execution or persistent plugin installation is claimed
by these tests. Startup wiring and node-backed service validation remain the next
activation gate.

### Disposable-node reverse service rehearsal

`reverse_service_regtest.py` exercises the actual quote, gate, controller and
background recovery scanner on isolated Knots/CLN regtest nodes. Its explicit
`reverse-service-regtest-v1` profile requires `regtest`/`xbt-regtest` identities
and `bcrt`/`xbtrt` invoices. It never changes the disabled live activation flag.
Only the market fetch uses a deterministic fixture; routing, signing, HTLCs,
restarts, settlement and balance checks use real nodes.

The service quotes a 1,500-sat BTC invoice through a private final-hop hint. The
448-block BTC route produces the same 598-block admission minimum and 622-block
XBT invoice as the live candidate timing policy. Successful and rejected payments
restart both operators while pending, recover in fresh processes and then run
the actual background scanner twice. All six channel-side balances, receiver
invoice state and the identity of the single BTC attempt are checked.

```sh
.venv/bin/python tools/blake2b/test_reverse_service_profile.py -v
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --fail-outgoing
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --abort-unspent
```

The cancellation case advances only XBT after admission, verifies the original
margin no longer permits spending, and returns the held XBT through
`abort-unspent` without any BTC attempt. The suite includes all three cases.
`tick` is an explicit single service step that can submit a prepared payment;
`recover` is recovery-only and cannot start a prepared payment. Both retain the
same profile activation and identity checks as `run`.

These are disposable regtest commands. They do not install a live gate, change
systemd services or enable the Phoenix payment. Persistent startup wiring and
live activation remain separate work.

### Explicit bounded live activation

Patch 0076 adds `reverse_activation.py`. The live profile remains disabled by
default unless the private service settings contain the exact activation record
created by `install`. It binds the existing operator/payer identities, RPC command
arrays, operator data directory and monitored swap root, with fixed 1,500 BTC sats,
500,000 XBT sats maximum, 30 BTC sats routing allowance, 1% margin and a 576-block
route cap. Existing quote validation, timing checks and recovery rules remain.

```sh
.venv/bin/python tools/blake2b/test_reverse_activation.py -v
.venv/bin/python tools/blake2b/reverse_activation.py install
systemctl --user restart cln-xbt-operator cln-swap-recovery
.venv/bin/python tools/blake2b/reverse_activation.py status
```

The installer only reads node RPCs and writes private settings. It requires the
existing XBT wallet, exact networks/identities, no pending HTLCs on the three nodes,
and both operator recovery reserves. It saves `settings.before-reverse-live.json`
before updating settings. It does not create a quote, pay, fund or close a channel.
Do not restore that backup to disable recovery while a reverse payment is pending.

Only the XBT operator launcher receives `--reverse-settings`. It creates an
executable wrapper at `<operator-root>/reverse-live-gate.py`, uses persistent
`reverse-live-gate.json` in the same directory, and loads it at every node start.
The receiver and BTC operator launcher arguments are unchanged. Existing wrapper
content that differs is refused. Unrelated incoming XBT HTLCs continue to CLN.
`status` checks the operator identity and the live gate's `reverse-pilot-info`
RPC, and prints only readiness and a held-HTLC count.

Manual `reverse_service.py` live commands read the same private settings (default
`~/.config/cln-swaps/settings.json`, overridable with `--settings`). Quote creation
and manual execution activate only within the calling operation. Execution checks
the directory and quote against the enabled service. The background worker scopes
activation to recovery of a bound existing record; prepared or absent controller
state still cannot originate a BTC attempt. Test profiles cannot substitute for
live networks or change the activation record.

After restart, check readiness and obtain a fresh read-only inspection of the
unpaid BTC invoice before creating a new quote. Quote expiry, stale prices,
insufficient route capacity or timing margin remain reasons to refuse payment.
Keep invoice files and quote output private. Never reuse a directory or discard
state after an uncertain payment outcome.

### Operator-only reverse quotes

Patch 0077 removes customer-wallet RPC access from service quote creation. The
service uses the already-bound payer node ID and reads its own XBT channel's
`receivable_msat`. Only BTC/XBT operator RPCs are used for inspection, route
planning, registration and signing. Existing admission, quote/channel binding,
fee, timing and recovery checks remain in force.

The read-only inspector supports the same boundary:

```sh
.venv/bin/python tools/blake2b/reverse_check.py \
  --operator-only \
  --invoice-file "$HOME/cln-live-pilot/unpaid-btc-invoice.txt" \
  --max-routing-fee-sats 30 --margin-bps 100 \
  --max-xbt-sats 500000 --max-delay 576
```

This is inspection only: a previously paid invoice must not be used for a new
payment. The example path is illustrative; use a fresh unpaid invoice when
creating a new quote. `--operator-only` reads the operator RPC arrays and bound
payer ID from the private service settings, optionally selected by `--settings`.
It reports `payer_rpc_checked: false` and `xbt_payer_spendable_sats: null`, rather
than pretending to know the remote wallet's spendable balance. The operator's
receivable capacity is an upper bound, not a promise that the customer can pay.
The original two-wallet diagnostic mode remains available without this flag.

This separates the quote/payment boundary, not the entire deployment yet. The
existing tower settings still name the test customer, and the optional receiver
service and recovery health check still monitor it. There is no public quote API,
arbitrary-payer admission, routed XBT ingress or multi-customer service in this
patch. Independent-customer configuration and packaging remain separate work.

The disposable-node service test rejects any quote-time RPC to the payer wallet
and checks that the generated quote explicitly reports that wallet as unobserved.
The harness itself still controls the disposable customer to fund and pay the test.

### Independent customer wallet deployment

Patch 0078 separates the tower's existing customer wallet from the exchange
operator target. It keeps wallet paths, node identities, channels and swap records
unchanged. It does not create, fund, pay or close anything.

```sh
.venv/bin/python tools/blake2b/test_separate_customer.py -v
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli
.venv/bin/python tools/blake2b/separate_customer.py
systemctl --user daemon-reload
systemctl --user enable cln-xbt-receiver.service
systemctl --user restart cln-swap-recovery.service
```

The migration validates existing generated units before writing, saves private
`settings.before-customer-separation.json`, and creates a separate private
`customer-wallet` configuration directory beside the operator settings. That
configuration contains only the customer launch paths and its required XBT
backend credentials. No wallet data is moved. Re-running the migration after an
interruption accepts only the expected original or completed configuration.
Unrelated modifications are refused rather than overwritten.

The operator settings use `deployment: operator-pair-v1`. They retain the known
customer node ID for quote/channel binding but contain no customer RPC command
or wallet data directory. Existing activation is rebound to the operator-only
configuration with the same limits and operator/customer identities. Saved swap
bindings stay identical, so historical recovery remains available.

`cln-swaps.target` now wants only `cln-btc-operator`, `cln-xbt-operator` and
`cln-swap-recovery`. The independent `cln-xbt-receiver` service has its own
`default.target` enablement and no `PartOf=cln-swaps.target`; stopping/restarting
the operator target no longer stops/restarts that customer wallet. Its new
configuration is used on its next start. No node restart is needed for migration.
Enable the independent customer unit so it still starts at user-manager startup.

Operator recovery reads only its own node RPCs. `nodes_ready` now refers to the
two operator nodes, `customer_wallet_managed` is false, and `xbt_connected` reports
whether the known customer appears connected in the operator's peer list. An
offline customer does not prevent existing swap reconciliation. The worker no
longer uses the customer's RPC or automatically reconnects it to localhost.
Ordinary node/channel reconnection remains CLN's responsibility.

This is deployment separation for the existing bounded single-customer pilot.
It does not add a public API, anonymous payer selection or concurrent customers.

### Customer review, pay and status

Patch 0079 adds a file-based customer interface. The customer needs their original
unpaid BTC invoice, a minimal offer from the operator, and access to their own
XBT CLN wallet. They do not receive operator RPC commands, internal routes or
controller state. This is a local/private file handoff, not a public HTTP API.
The existing bounded pilot still requires a direct XBT channel to the operator.

After creating a fresh operator quote, export its customer offer:

```sh
.venv/bin/python tools/blake2b/reverse_service.py offer \
  --directory /path/to/operator-swap --output /path/to/customer-offer.json
```

Give the customer that private offer file. Do not give them `reverse-quote.json`.
The operator runs the existing `reverse_service.py run --directory ...` command
for the authorized swap; recovery alone still cannot originate a new BTC payment.

On the customer side, review using the customer's own copy of the BTC invoice:

```sh
.venv/bin/python tools/blake2b/reverse_customer.py review \
  --offer-file /path/to/customer-offer.json \
  --btc-invoice-file /path/to/unpaid-btc-invoice.txt \
  --lightning-dir "$HOME/cln-xbt-peer" \
  --directory /path/to/new-customer-payment \
  --max-xbt-sats 400000 --max-delay 2016
```

The review checks both signed invoices, currencies, exact amounts, matching
payment hashes, expiry, customer-approved XBT and locktime caps, and the customer's
ready direct channel to the XBT invoice payee. It records the reviewed offer
privately and prints only amounts, locktime and expiry. It never submits payment.
The operator's minimal offer contains an SHA256 fingerprint of the exact original
BTC invoice, the signed XBT invoice, amounts and expiry. Customer and operator
state directories are distinct and must never be substituted for each other.

After reviewing the amounts, the customer explicitly submits once:

```sh
.venv/bin/python tools/blake2b/reverse_customer.py pay \
  --directory /path/to/new-customer-payment
.venv/bin/python tools/blake2b/reverse_customer.py status \
  --directory /path/to/new-customer-payment
```

Submission is locked and checkpointed before the customer wallet's `pay` RPC,
with zero XBT routing fee allowance for the direct channel and retries disabled.
A second invocation reads the original payment status rather than calling `pay`
again, including after failure, timeout or a missing payment record. An unknown
outcome requires inspection; it never authorizes another submission. Status is
read-only. Successful status verifies the preimage, payment hash and exact sent
amount without printing the proof, invoices or node identities. Keep the customer
state directory after completion and do not reuse paid BTC invoices.

The disposable service rehearsal now exports an offer, reviews and pays through
the customer tool, restarts operators, reconciles through background recovery,
and checks customer proof plus all six channel-side balances. Success, rejection
and unspent-cancellation modes cover this interface. No live transfer is required
to test it.

### Authenticated loopback quote API

Patch 0080 adds `reverse_quote_api.py` and `reverse_request.py`. This first API
listens only on `127.0.0.1`, uses a private bearer credential bound to the existing
customer identity, and exposes only `POST /v1/quote`. It cannot start a controller,
pay, refund, close a channel, change limits or administer nodes. It is not a public
exchange server. Do not expose it through a port forward or reverse proxy yet.

Initialize the credential without printing it, then run the foreground listener:

```sh
.venv/bin/python tools/blake2b/reverse_quote_api.py init \
  --token-file "$HOME/.config/cln-swaps/customer-api.json"
.venv/bin/python tools/blake2b/reverse_quote_api.py serve \
  --token-file "$HOME/.config/cln-swaps/customer-api.json"
```

The customer requests a quote with a fresh unpaid 1,500-sat BTC invoice:

```sh
.venv/bin/python tools/blake2b/reverse_request.py \
  --invoice-file /path/to/unpaid-btc-invoice.txt \
  --token-file /path/to/customer-api.json \
  --directory /path/to/new-customer-request \
  --max-xbt-sats 400000
```

The requester saves `offer.json` privately in that directory and prints only
amounts, expiry and the fact that customer review is required. Continue with
`reverse_customer.py review`, using that offer and the customer's original BTC
invoice, and then explicitly `pay` if accepted. Customer review verifies signed
invoice contents; receiving an API response is not itself payment approval.
The operator still explicitly runs the authorized swap controller. Existing
recovery never originates a new outgoing payment.

Each request carries a random ID persisted before transmission. The operator
journals that ID, request contents and operator/customer binding before quote
creation. Identical retries return the same published offer, even after a lost
reply; changed contents or identities are refused. If creation stopped before a
signed quote was published, retries require operator inspection rather than
creating another quote. Expired offers are not automatically renewed. Never delete
request records merely to retry an ambiguous operation.

The server rejects missing/wrong credentials before parsing bodies, browser
Origin headers, unexpected Host values, transfer encoding, non-JSON requests and
oversized bodies. It emits no access logs or raw exception text. The client uses
no environment proxy, refuses redirects and sends credentials only to an explicit
loopback HTTP endpoint. Credential files must remain private (0600). Remote TLS
transport, credential lifecycle, multi-customer limits and unattended serving are
not included in this patch.

Tests use real localhost HTTP for authentication and request replay. The
node-backed service rehearsal now requests the quote twice over HTTP, then uses
the resulting offer in the customer review/pay/status flow; no duplicate quote
or payment is permitted. The listener is stopped after quote acquisition, showing
that payment recovery does not depend on the quote HTTP connection remaining up.

### Authorized automatic processing

Patch 0081 lets the background worker originate the bounded BTC payment only when
an operator authorization exists for that exact signed quote. Without one, behavior
is unchanged: recovery alone cannot start a prepared payment. The authorization
contains a SHA256 digest of the entire private quote and its expiry. Directory,
operator/customer identities, quote profile and all existing limits remain bound.
The quote digest is checked again inside the service lock before processing.

An operator can authorize one existing signed quote explicitly:

```sh
.venv/bin/python tools/blake2b/reverse_authorize.py \
  --directory /path/to/operator-swap
```

Authorization records permission; it does not itself call a payment RPC. The
worker waits for the original gate to hold the matching XBT HTLC. Existing
pre-spend checks then revalidate identity, amount, invoice, liquidity, fees,
reserves and timing before the original BTC submission. The outgoing checkpoint
is still saved before sendpay, and pending or uncertain attempts are never resent.

Alternatively, the operator can authorize new quotes issued by one API listener:

```sh
.venv/bin/python tools/blake2b/reverse_quote_api.py serve \
  --token-file "$HOME/.config/cln-swaps/customer-api.json" --auto-process
```

The flag is off by default. Its value is pinned in each new request journal before
quote creation, and authorization is persisted before the offer is returned.
Restarting a listener with the flag does not upgrade older quote-only requests.
Conversely, restarting without it does not revoke authorization already recorded
for issued quotes. Interrupted requests may finish recording their original
approval, but cannot create another quote automatically. The customer API exposes
no endpoint to grant or broaden authorization.

Expired or changed approval blocks new automatic spending. It does not block
recovery of an attempt already submitted, because that recovery may be required
to settle or refund the original HTLC. A prepared swap refused by preflight may
still need operator inspection and `abort-unspent`; expiry alone is never treated
as proof that a submitted payment failed. Preserve all quote/controller records.

After this patch is validated, restarting only `cln-swap-recovery` loads the new
worker behavior. Existing unapproved quotes remain recovery-only. The HTTP API
continues to bind localhost only. This is still the fixed 1,500-sat BTC pilot for
one known customer, not a general unattended public exchange.

Disposable-node automatic-processing tests:

```sh
.venv/bin/python tools/blake2b/test_reverse_authorize.py -v
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --auto-process
.venv/bin/python tools/blake2b/reverse_service_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --auto-process --fail-outgoing
```

They authorize through the HTTP quote path and let a fresh background worker
start the original BTC attempt. No manual service start is used. Operator restart,
recovery, success/rejection, customer payment proof and all channel balances remain
checked. Both modes are included in the regression catalog.


### Persistent localhost quote API

Run the installer using the project venv after customer separation. It uses the
configured Python and existing private operator settings, creates or validates
`~/.config/cln-swaps/customer-api.json`, and writes `cln-swap-quotes.service`.
The token value is never placed in the unit or printed. No services or payments
are started by installation. Repeating identical installation is safe; a changed
unit or mode is refused for local inspection.

```sh
.venv/bin/python tools/blake2b/quote_api_service.py --auto-process
systemctl --user daemon-reload
systemctl --user restart cln-swap-recovery.service
systemctl --user enable --now cln-swap-quotes.service
systemctl --user is-active cln-swap-quotes.service cln-swap-recovery.service
```

Omit `--auto-process` for quote-only operation. With it, new quotes carry the
per-quote authorization from patch 0081. Existing quotes are not retroactively
authorized. Already authorized or started swaps retain their recovery obligations
if the listener stops. Stop new requests with `systemctl --user stop
cln-swap-quotes.service`; keep the recovery worker running.

The listener remains bound to 127.0.0.1:19840 (optional `--port`). It is enabled
under the existing cln-swaps target and does not manage the customer wallet.
This remains the bounded single-customer pilot, not a public exchange service.
An active process only confirms that the listener is running; quote eligibility,
routes, liquidity and live activation are checked when requests arrive.


### One-command customer payment

`customer_swap.py` combines quote request, local wallet review and explicit
confirmation. Use a fresh unpaid 1,500-sat BTC invoice saved in a private file,
an available customer XBT balance, and the running localhost API. For example:

```sh
.venv/bin/python tools/blake2b/customer_swap.py \
  --invoice-file "$HOME/cln-live-pilot/fresh-btc-invoice.txt" \
  --lightning-dir "$HOME/cln-xbt-peer" \
  --directory "$HOME/cln-live-pilot/customer-payment-1" \
  --max-xbt-sats 400000
```

The default token file is `~/.config/cln-swaps/customer-api.json`. This credential
allows requesting quotes only; the command never reads operator settings or uses
operator RPC. The customer uses its own CLN RPC to verify both invoices and pay.
Enter exactly `PAY` to accept the displayed XBT amount. Any other answer leaves
the payment unsubmitted. There is no automatic acceptance flag.

Repeat the same command and directory after interruption. Before submission it
reuses the recorded offer and asks again after fresh validation; after submission
it only queries the wallet. Pending or unknown outcomes must be inspected using
the same directory, never by making a new attempt directory. This wrapper does
not poll indefinitely: repeat it to check a pending payment. Expired unsubmitted
offers fail validation and do not cause a replacement quote or payment.

The existing fixed 1,500-sat pilot and 500,000-XBT-sat maximum remain in force.
The default incoming locktime cap remains 2,016 XBT blocks. The live API must have
automatic processing enabled for the unattended operator workflow.


### Clear refusals and explicit quote retries

The API records selected definite read-only preflight refusals as `refused`:
insufficient incoming XBT liquidity, insufficient outgoing BTC liquidity,
price above the cap, insufficient operator reserves, or no route within limits.
The customer receives a fixed public reason code and message, without raw RPC
errors or invoice details. No quote is registered or payment authorized by these
refusals. Unexpected failures still require inspection.

After correcting a definite refusal, rerun the same `customer_swap.py` command
with `--retry-quote`. This retries the same request ID only if its operator journal
records `refused` and no quote directory exists. Each new attempt first records
`creating`; a crash or uncertain response cannot silently permit another attempt.
A published offer is returned unchanged, and existing customer payment intents
still use the original single-submission guards. The operator's original
per-request automatic-processing choice is retained across retries.

Old requests left in `creating` (including the earlier liquidity failure) cannot
be upgraded to definite refusals based only on missing files. Do not delete them
or use `--retry-quote` to bypass inspection. Completed customer directories remain
status-only, even if the retry flag is supplied.

Restart the quote listener after applying this patch:

```sh
systemctl --user restart cln-swap-quotes.service
```

This does not restart either Lightning node or the recovery worker.
